"""
tests/unit/providers/adapters/test_angel_one.py

Unit tests for the Angel One SmartAPI provider adapter.

Covers:
- Authentication flow (TOTP generation, JWT storage)
- Historical OHLCV fetch with correct interval mapping
- HTTP 401 triggers re-authentication and retries once
- HTTP 429 raises ProviderRateLimitedError (circuit breaker NOT counted)
- Live quote fetch returns correct fields
- Credentials NEVER appear in log output (caplog verification)
- Broker analytics: PCR, OI buildup, gainers/losers
- 3m interval blocked (ProviderUnsupportedError)
- Unsupported interval blocked (ProviderUnsupportedError)

Requirements: 5.9, 19.7
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.providers.adapters.angel_one import (
    AngelOneAdapter,
    _INTERVAL_MAP,
    _parse_retry_after,
)
from src.providers.adapters.base import (
    ProviderAuthError,
    ProviderDataError,
    ProviderMarketClosedError,
    ProviderRateLimitedError,
    ProviderUnavailableError,
    ProviderUnsupportedError,
)

# ---------------------------------------------------------------------------
# Constants used across tests
# ---------------------------------------------------------------------------

_API_KEY = "test_api_key_value"
_CLIENT_ID = "C123456"
_TOTP_SECRET = "JBSWY3DPEHPK3PXP"  # well-known test TOTP seed (base32)

# A valid-looking JWT access token — must NEVER appear in logs
_JWT_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.test.signature"

# A valid login success response from SmartAPI
_LOGIN_SUCCESS_BODY = {
    "status": True,
    "message": "SUCCESS",
    "data": {
        "jwtToken": _JWT_TOKEN,
        "refreshToken": "refresh_token_value",
        "feedToken": "feed_token_value",
    },
}

# A minimal historical OHLCV success response
_CANDLE_SUCCESS_BODY = {
    "status": True,
    "message": "SUCCESS",
    "data": [
        ["2024-01-15T09:15:00+05:30", 22100.0, 22150.0, 22080.0, 22130.0, 123456],
        ["2024-01-15T09:16:00+05:30", 22130.0, 22160.0, 22110.0, 22145.0, 98765],
    ],
}

# A minimal live quote success response
_QUOTE_SUCCESS_BODY = {
    "status": True,
    "message": "SUCCESS",
    "data": {
        "fetched": [
            {
                "tradingsymbol": "NIFTY",
                "symboltoken": "99926000",
                "exchange": "NSE",
                "ltp": 22150.5,
                "open": 22100.0,
                "high": 22200.0,
                "low": 22050.0,
                "close": 22130.0,
                "volume": 9876543,
                "tradeTime": "2024-01-15T09:30:00+05:30",
                "upperCircuit": 24343.0,
                "lowerCircuit": 19917.0,
                "52WeekHigh": 22780.0,
                "52WeekLow": 18837.0,
                "buyQty": 500000,
                "sellQty": 480000,
                "oi": None,
                "tradedValue": 123456789.0,
            }
        ]
    },
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_response(
    status_code: int = 200,
    json_body: Any = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    """Build a minimal httpx.Response for use in mock assertions."""
    import json as _json

    content = _json.dumps(json_body or {}).encode()
    return httpx.Response(
        status_code=status_code,
        headers=headers or {},
        content=content,
    )


def _make_adapter(http_client: httpx.AsyncClient | None = None) -> AngelOneAdapter:
    """Construct an AngelOneAdapter with test credentials."""
    return AngelOneAdapter(
        api_key=_API_KEY,
        client_id=_CLIENT_ID,
        totp_secret=_TOTP_SECRET,
        http_client=http_client,
    )


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


class TestAuthentication:
    """Test TOTP + JWT authentication flow."""

    @pytest.mark.asyncio
    async def test_authenticate_stores_jwt_token(self) -> None:
        """Successful login stores the jwtToken from the response body."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(
            return_value=_make_response(200, _LOGIN_SUCCESS_BODY)
        )

        adapter = _make_adapter(mock_client)
        await adapter.authenticate()

        assert adapter._access_token == _JWT_TOKEN
        assert adapter._refresh_token == "refresh_token_value"
        assert adapter._token_acquired_at is not None

    @pytest.mark.asyncio
    async def test_authenticate_stores_refresh_token(self) -> None:
        """Successful login also stores the refreshToken."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(
            return_value=_make_response(200, _LOGIN_SUCCESS_BODY)
        )

        adapter = _make_adapter(mock_client)
        await adapter.authenticate()

        assert adapter._refresh_token == "refresh_token_value"

    @pytest.mark.asyncio
    async def test_authenticate_uses_totp_code_in_payload(self) -> None:
        """Login request body contains the TOTP code as 'password' and 'totp' fields."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(
            return_value=_make_response(200, _LOGIN_SUCCESS_BODY)
        )

        adapter = _make_adapter(mock_client)

        with patch("pyotp.TOTP") as mock_totp_class:
            mock_totp = MagicMock()
            mock_totp.now.return_value = "123456"
            mock_totp_class.return_value = mock_totp

            await adapter.authenticate()

        call_kwargs = mock_client.post.call_args.kwargs
        assert call_kwargs["json"]["password"] == "123456"
        assert call_kwargs["json"]["totp"] == "123456"
        assert call_kwargs["json"]["clientcode"] == _CLIENT_ID

    @pytest.mark.asyncio
    async def test_authenticate_raises_auth_error_on_api_rejection(self) -> None:
        """When SmartAPI returns status=False, ProviderAuthError is raised."""
        body = {"status": False, "message": "Invalid credentials", "data": None}
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(return_value=_make_response(200, body))

        adapter = _make_adapter(mock_client)
        with pytest.raises(ProviderAuthError, match="Invalid credentials"):
            await adapter.authenticate()

    @pytest.mark.asyncio
    async def test_authenticate_raises_auth_error_on_http_401(self) -> None:
        """HTTP 401 from login endpoint raises ProviderAuthError."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(
            return_value=_make_response(401, {"status": False, "message": "Unauthorised"})
        )

        adapter = _make_adapter(mock_client)
        with pytest.raises(ProviderAuthError):
            await adapter.authenticate()

    @pytest.mark.asyncio
    async def test_authenticate_raises_unavailable_on_server_error(self) -> None:
        """HTTP 5xx during login raises ProviderUnavailableError."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(
            return_value=_make_response(503, {"status": False})
        )

        adapter = _make_adapter(mock_client)
        with pytest.raises(ProviderUnavailableError):
            await adapter.authenticate()

    @pytest.mark.asyncio
    async def test_authenticate_raises_unavailable_on_timeout(self) -> None:
        """Network timeout during login raises ProviderUnavailableError."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(side_effect=httpx.TimeoutException("timeout"))

        adapter = _make_adapter(mock_client)
        with pytest.raises(ProviderUnavailableError):
            await adapter.authenticate()

    @pytest.mark.asyncio
    async def test_authenticate_raises_auth_error_when_token_missing(self) -> None:
        """Login response with no jwtToken raises ProviderAuthError."""
        body = {
            "status": True,
            "message": "SUCCESS",
            "data": {"refreshToken": "refresh"},  # no jwtToken
        }
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(return_value=_make_response(200, body))

        adapter = _make_adapter(mock_client)
        with pytest.raises(ProviderAuthError, match="jwtToken missing"):
            await adapter.authenticate()

    @pytest.mark.asyncio
    async def test_ensure_authenticated_calls_authenticate_when_no_token(self) -> None:
        """ensure_authenticated() triggers login when no token is held."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(
            return_value=_make_response(200, _LOGIN_SUCCESS_BODY)
        )

        adapter = _make_adapter(mock_client)
        assert adapter._access_token is None
        await adapter.ensure_authenticated()
        assert adapter._access_token == _JWT_TOKEN

    @pytest.mark.asyncio
    async def test_ensure_authenticated_skips_login_when_token_present(self) -> None:
        """ensure_authenticated() does NOT call login if a token is already held."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(
            return_value=_make_response(200, _LOGIN_SUCCESS_BODY)
        )

        adapter = _make_adapter(mock_client)
        adapter._access_token = _JWT_TOKEN  # pre-set token

        await adapter.ensure_authenticated()

        mock_client.post.assert_not_called()

    @pytest.mark.asyncio
    async def test_rotate_token_forces_fresh_login(self) -> None:
        """rotate_token() clears the existing token and authenticates again."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(
            return_value=_make_response(200, _LOGIN_SUCCESS_BODY)
        )

        adapter = _make_adapter(mock_client)
        adapter._access_token = "old_token"
        await adapter.rotate_token()

        assert adapter._access_token == _JWT_TOKEN
        mock_client.post.assert_called_once()


# ---------------------------------------------------------------------------
# Credentials never appear in logs
# ---------------------------------------------------------------------------


class TestCredentialSecrecy:
    """Verify that no credential ever appears in log output."""

    @pytest.mark.asyncio
    async def test_credentials_not_in_logs_during_authenticate(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """API key, TOTP secret, and JWT token must not appear in any log record."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(
            return_value=_make_response(200, _LOGIN_SUCCESS_BODY)
        )

        adapter = _make_adapter(mock_client)

        with caplog.at_level(logging.DEBUG):
            await adapter.authenticate()

        full_log = " ".join(r.getMessage() for r in caplog.records)
        full_log += " ".join(str(r.__dict__) for r in caplog.records)

        assert _API_KEY not in full_log, "API key must not appear in logs"
        assert _TOTP_SECRET not in full_log, "TOTP secret must not appear in logs"
        assert _JWT_TOKEN not in full_log, "JWT access token must not appear in logs"

    @pytest.mark.asyncio
    async def test_credentials_not_in_logs_during_request(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """API key and JWT token must not appear in any log during an API call."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(
            return_value=_make_response(200, _LOGIN_SUCCESS_BODY)
        )
        mock_client.request = AsyncMock(
            return_value=_make_response(200, _CANDLE_SUCCESS_BODY)
        )

        adapter = _make_adapter(mock_client)

        with caplog.at_level(logging.DEBUG):
            await adapter.fetch_historical_ohlcv(
                symbol="RELIANCE",
                token="3045",
                from_date="2024-01-15 09:15",
                to_date="2024-01-15 15:30",
                interval="1m",
            )

        full_log = " ".join(r.getMessage() for r in caplog.records)
        full_log += " ".join(str(r.__dict__) for r in caplog.records)

        assert _API_KEY not in full_log, "API key must not appear in logs"
        assert _JWT_TOKEN not in full_log, "JWT token must not appear in logs"
        assert _TOTP_SECRET not in full_log, "TOTP secret must not appear in logs"

    @pytest.mark.asyncio
    async def test_credentials_not_in_logs_on_auth_error(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Even when authentication fails, credentials must not be logged."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(
            return_value=_make_response(
                401, {"status": False, "message": "Unauthorised"}
            )
        )

        adapter = _make_adapter(mock_client)

        with caplog.at_level(logging.DEBUG):
            with pytest.raises(ProviderAuthError):
                await adapter.authenticate()

        full_log = " ".join(r.getMessage() for r in caplog.records)
        full_log += " ".join(str(r.__dict__) for r in caplog.records)

        assert _API_KEY not in full_log
        assert _TOTP_SECRET not in full_log


# ---------------------------------------------------------------------------
# Historical OHLCV
# ---------------------------------------------------------------------------


class TestFetchHistoricalOHLCV:
    """Test historical OHLCV candle fetch behaviour."""

    @pytest.mark.asyncio
    async def test_returns_parsed_candles(self) -> None:
        """Returns a list of dicts with OHLCV fields."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(
            return_value=_make_response(200, _LOGIN_SUCCESS_BODY)
        )
        mock_client.request = AsyncMock(
            return_value=_make_response(200, _CANDLE_SUCCESS_BODY)
        )

        adapter = _make_adapter(mock_client)
        result = await adapter.fetch_historical_ohlcv(
            symbol="RELIANCE",
            token="3045",
            from_date="2024-01-15 09:15",
            to_date="2024-01-15 15:30",
            interval="1m",
        )

        assert len(result) == 2
        assert result[0]["open"] == 22100.0
        assert result[0]["high"] == 22150.0
        assert result[0]["low"] == 22080.0
        assert result[0]["close"] == 22130.0
        assert result[0]["volume"] == 123456

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "canonical,expected_smartapi",
        [
            ("1m", "ONE_MINUTE"),
            ("5m", "FIVE_MINUTE"),
            ("10m", "TEN_MINUTE"),
            ("15m", "FIFTEEN_MINUTE"),
            ("30m", "THIRTY_MINUTE"),
            ("1h", "ONE_HOUR"),
            ("1d", "ONE_DAY"),
            ("1w", "ONE_WEEK"),
        ],
    )
    async def test_interval_mapping(
        self, canonical: str, expected_smartapi: str
    ) -> None:
        """Each canonical interval maps to the correct SmartAPI interval name."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(
            return_value=_make_response(200, _LOGIN_SUCCESS_BODY)
        )
        mock_client.request = AsyncMock(
            return_value=_make_response(200, _CANDLE_SUCCESS_BODY)
        )

        adapter = _make_adapter(mock_client)
        await adapter.fetch_historical_ohlcv(
            symbol="NIFTY",
            token="99926000",
            from_date="2024-01-01 09:15",
            to_date="2024-01-31 15:30",
            interval=canonical,
        )

        request_call = mock_client.request.call_args
        sent_body = request_call.kwargs.get("json") or request_call.args[3] if request_call.args else request_call.kwargs.get("json")
        # Retrieve from kwargs
        actual_body = mock_client.request.call_args.kwargs.get("json") or {}
        assert actual_body.get("interval") == expected_smartapi

    @pytest.mark.asyncio
    async def test_3m_interval_raises_unsupported_error(self) -> None:
        """3m interval is permanently blocked — raises ProviderUnsupportedError."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(
            return_value=_make_response(200, _LOGIN_SUCCESS_BODY)
        )

        adapter = _make_adapter(mock_client)
        with pytest.raises(ProviderUnsupportedError, match="3m"):
            await adapter.fetch_historical_ohlcv(
                symbol="NIFTY",
                token="99926000",
                from_date="2024-01-01 09:15",
                to_date="2024-01-31 15:30",
                interval="3m",
            )

        # Verify no API call was made
        mock_client.request.assert_not_called()

    @pytest.mark.asyncio
    async def test_unsupported_interval_raises_unsupported_error(self) -> None:
        """Unknown interval raises ProviderUnsupportedError."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(
            return_value=_make_response(200, _LOGIN_SUCCESS_BODY)
        )

        adapter = _make_adapter(mock_client)
        with pytest.raises(ProviderUnsupportedError):
            await adapter.fetch_historical_ohlcv(
                symbol="NIFTY",
                token="99926000",
                from_date="2024-01-01 09:15",
                to_date="2024-01-31 15:30",
                interval="2h",  # valid for crypto Binance, not Angel One
            )

    @pytest.mark.asyncio
    async def test_http_401_triggers_reauth_and_retry(self) -> None:
        """HTTP 401 on first request triggers re-authentication and a second attempt."""
        auth_response = _make_response(200, _LOGIN_SUCCESS_BODY)
        first_request = _make_response(401, {"status": False, "message": "Expired"})
        second_request = _make_response(200, _CANDLE_SUCCESS_BODY)

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(return_value=auth_response)
        mock_client.request = AsyncMock(side_effect=[first_request, second_request])

        adapter = _make_adapter(mock_client)
        result = await adapter.fetch_historical_ohlcv(
            symbol="NIFTY",
            token="99926000",
            from_date="2024-01-01 09:15",
            to_date="2024-01-01 15:30",
            interval="1m",
        )

        # Should have made 2 data requests (first 401, then retry after re-auth)
        assert mock_client.request.call_count == 2
        # Result from the second (successful) request
        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_http_401_does_not_retry_infinitely(self) -> None:
        """Second 401 (after re-auth) raises ProviderAuthError immediately."""
        auth_response = _make_response(200, _LOGIN_SUCCESS_BODY)
        always_401 = _make_response(401, {"status": False, "message": "Expired"})

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(return_value=auth_response)
        mock_client.request = AsyncMock(return_value=always_401)

        adapter = _make_adapter(mock_client)
        with pytest.raises(ProviderAuthError):
            await adapter.fetch_historical_ohlcv(
                symbol="NIFTY",
                token="99926000",
                from_date="2024-01-01 09:15",
                to_date="2024-01-01 15:30",
                interval="1m",
            )

        # Only 2 requests: initial + one retry
        assert mock_client.request.call_count == 2

    @pytest.mark.asyncio
    async def test_http_429_raises_rate_limited_error(self) -> None:
        """HTTP 429 raises ProviderRateLimitedError — no retry."""
        auth_response = _make_response(200, _LOGIN_SUCCESS_BODY)
        rate_limited = _make_response(
            429, {"status": False}, headers={"Retry-After": "30"}
        )

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(return_value=auth_response)
        mock_client.request = AsyncMock(return_value=rate_limited)

        adapter = _make_adapter(mock_client)
        with pytest.raises(ProviderRateLimitedError) as exc_info:
            await adapter.fetch_historical_ohlcv(
                symbol="NIFTY",
                token="99926000",
                from_date="2024-01-01 09:15",
                to_date="2024-01-01 15:30",
                interval="1m",
            )

        assert exc_info.value.retry_after_s == 30
        assert exc_info.value.status_code == 429
        # Only 1 data request — no retry on 429
        assert mock_client.request.call_count == 1

    @pytest.mark.asyncio
    async def test_http_429_without_retry_after_header(self) -> None:
        """HTTP 429 with no Retry-After header sets retry_after_s to None."""
        auth_response = _make_response(200, _LOGIN_SUCCESS_BODY)
        rate_limited = _make_response(429, {"status": False})

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(return_value=auth_response)
        mock_client.request = AsyncMock(return_value=rate_limited)

        adapter = _make_adapter(mock_client)
        with pytest.raises(ProviderRateLimitedError) as exc_info:
            await adapter.fetch_historical_ohlcv(
                symbol="NIFTY",
                token="99926000",
                from_date="2024-01-01 09:15",
                to_date="2024-01-01 15:30",
                interval="1m",
            )

        assert exc_info.value.retry_after_s is None

    @pytest.mark.asyncio
    async def test_server_error_raises_unavailable(self) -> None:
        """HTTP 5xx raises ProviderUnavailableError."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(return_value=_make_response(200, _LOGIN_SUCCESS_BODY))
        mock_client.request = AsyncMock(
            return_value=_make_response(503, {"status": False})
        )

        adapter = _make_adapter(mock_client)
        with pytest.raises(ProviderUnavailableError):
            await adapter.fetch_historical_ohlcv(
                symbol="NIFTY",
                token="99926000",
                from_date="2024-01-01 09:15",
                to_date="2024-01-01 15:30",
                interval="1m",
            )

    @pytest.mark.asyncio
    async def test_empty_data_when_market_closed(self) -> None:
        """'No data' error message from SmartAPI raises ProviderMarketClosedError."""
        body = {"status": False, "message": "No Data Available for given interval", "data": None}

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(return_value=_make_response(200, _LOGIN_SUCCESS_BODY))
        mock_client.request = AsyncMock(return_value=_make_response(200, body))

        adapter = _make_adapter(mock_client)
        with pytest.raises(ProviderMarketClosedError):
            await adapter.fetch_historical_ohlcv(
                symbol="NIFTY",
                token="99926000",
                from_date="2024-01-07 09:15",  # Sunday
                to_date="2024-01-07 15:30",
                interval="1m",
            )

    @pytest.mark.asyncio
    async def test_candles_include_provenance_metadata(self) -> None:
        """Each returned candle dict includes provider and sourceType metadata."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(return_value=_make_response(200, _LOGIN_SUCCESS_BODY))
        mock_client.request = AsyncMock(return_value=_make_response(200, _CANDLE_SUCCESS_BODY))

        adapter = _make_adapter(mock_client)
        result = await adapter.fetch_historical_ohlcv(
            symbol="RELIANCE",
            token="3045",
            from_date="2024-01-15 09:15",
            to_date="2024-01-15 15:30",
            interval="5m",
        )

        for candle in result:
            assert candle["provider"] == "angel_one"
            assert candle["sourceType"] == "BROKER_AUTHENTICATED"
            assert candle["symbol"] == "RELIANCE"
            assert candle["exchange"] == "NSE"
            assert candle["interval"] == "5m"

    @pytest.mark.asyncio
    async def test_malformed_candle_rows_are_skipped(self) -> None:
        """Candle rows with fewer than 6 elements are skipped without raising."""
        body = {
            "status": True,
            "message": "SUCCESS",
            "data": [
                ["2024-01-15T09:15:00+05:30", 22100.0, 22150.0, 22080.0, 22130.0, 123456],
                ["2024-01-15T09:16:00+05:30"],  # malformed — too short
                ["2024-01-15T09:17:00+05:30", 22140.0, 22170.0, 22120.0, 22155.0, 99000],
            ],
        }

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(return_value=_make_response(200, _LOGIN_SUCCESS_BODY))
        mock_client.request = AsyncMock(return_value=_make_response(200, body))

        adapter = _make_adapter(mock_client)
        result = await adapter.fetch_historical_ohlcv(
            symbol="NIFTY",
            token="99926000",
            from_date="2024-01-15 09:15",
            to_date="2024-01-15 09:18",
            interval="1m",
        )

        # Only 2 valid candles; malformed row is skipped
        assert len(result) == 2


# ---------------------------------------------------------------------------
# Live quote
# ---------------------------------------------------------------------------


class TestFetchLiveQuote:
    """Test live quote fetch behaviour."""

    @pytest.mark.asyncio
    async def test_returns_quote_with_expected_fields(self) -> None:
        """Live quote response contains LTP and other market data fields."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(return_value=_make_response(200, _LOGIN_SUCCESS_BODY))
        mock_client.request = AsyncMock(return_value=_make_response(200, _QUOTE_SUCCESS_BODY))

        adapter = _make_adapter(mock_client)
        result = await adapter.fetch_live_quote(token="99926000", exchange="NSE")

        assert result["ltp"] == 22150.5
        assert result["open"] == 22100.0
        assert result["high"] == 22200.0
        assert result["low"] == 22050.0
        assert result["volume"] == 9876543

    @pytest.mark.asyncio
    async def test_live_quote_includes_provenance_metadata(self) -> None:
        """Live quote result includes provider and sourceType metadata."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(return_value=_make_response(200, _LOGIN_SUCCESS_BODY))
        mock_client.request = AsyncMock(return_value=_make_response(200, _QUOTE_SUCCESS_BODY))

        adapter = _make_adapter(mock_client)
        result = await adapter.fetch_live_quote(token="99926000", exchange="NSE")

        assert result["provider"] == "angel_one"
        assert result["sourceType"] == "BROKER_AUTHENTICATED"
        assert result["exchange"] == "NSE"
        assert result["token"] == "99926000"

    @pytest.mark.asyncio
    async def test_empty_fetched_data_raises_market_closed(self) -> None:
        """Empty 'fetched' list raises ProviderMarketClosedError."""
        body = {
            "status": True,
            "message": "SUCCESS",
            "data": {"fetched": []},
        }
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(return_value=_make_response(200, _LOGIN_SUCCESS_BODY))
        mock_client.request = AsyncMock(return_value=_make_response(200, body))

        adapter = _make_adapter(mock_client)
        with pytest.raises(ProviderMarketClosedError):
            await adapter.fetch_live_quote(token="99926000")

    @pytest.mark.asyncio
    async def test_live_quote_401_triggers_reauth_and_retry(self) -> None:
        """HTTP 401 on quote request triggers re-auth and retries."""
        auth_response = _make_response(200, _LOGIN_SUCCESS_BODY)
        first_request = _make_response(401, {"status": False})
        second_request = _make_response(200, _QUOTE_SUCCESS_BODY)

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(return_value=auth_response)
        mock_client.request = AsyncMock(side_effect=[first_request, second_request])

        adapter = _make_adapter(mock_client)
        result = await adapter.fetch_live_quote(token="99926000")

        assert result["ltp"] == 22150.5
        assert mock_client.request.call_count == 2

    @pytest.mark.asyncio
    async def test_live_quote_429_raises_rate_limited(self) -> None:
        """HTTP 429 on quote raises ProviderRateLimitedError."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(return_value=_make_response(200, _LOGIN_SUCCESS_BODY))
        mock_client.request = AsyncMock(
            return_value=_make_response(429, {}, headers={"Retry-After": "60"})
        )

        adapter = _make_adapter(mock_client)
        with pytest.raises(ProviderRateLimitedError) as exc_info:
            await adapter.fetch_live_quote(token="99926000")

        assert exc_info.value.retry_after_s == 60


# ---------------------------------------------------------------------------
# Broker analytics
# ---------------------------------------------------------------------------


class TestFetchPCR:
    """Test Put-Call Ratio (PCR) fetch."""

    @pytest.mark.asyncio
    async def test_fetch_pcr_returns_data_with_metadata(self) -> None:
        """PCR response includes provider and sourceType metadata."""
        pcr_body = {
            "status": True,
            "message": "SUCCESS",
            "data": {"pcrOi": 0.82, "pcrVolume": 0.77},
        }
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(return_value=_make_response(200, _LOGIN_SUCCESS_BODY))
        mock_client.request = AsyncMock(return_value=_make_response(200, pcr_body))

        adapter = _make_adapter(mock_client)
        result = await adapter.fetch_pcr()

        assert result["pcrOi"] == 0.82
        assert result["provider"] == "angel_one"
        assert result["sourceType"] == "BROKER_AUTHENTICATED"
        assert "fetchedAt" in result

    @pytest.mark.asyncio
    async def test_fetch_pcr_api_error_raises_data_error(self) -> None:
        """PCR endpoint returning status=False raises ProviderDataError."""
        body = {"status": False, "message": "Service unavailable", "data": None}
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(return_value=_make_response(200, _LOGIN_SUCCESS_BODY))
        mock_client.request = AsyncMock(return_value=_make_response(200, body))

        adapter = _make_adapter(mock_client)
        with pytest.raises(ProviderDataError, match="Service unavailable"):
            await adapter.fetch_pcr()


class TestFetchOIBuildup:
    """Test OI buildup fetch."""

    @pytest.mark.asyncio
    async def test_fetch_oi_buildup_returns_records_with_metadata(self) -> None:
        """OI buildup records each include provider and sourceType metadata."""
        oi_body = {
            "status": True,
            "message": "SUCCESS",
            "data": [
                {"symbol": "RELIANCE", "builtupType": "Long Buildup", "oiChange": 12345},
                {"symbol": "TCS", "builtupType": "Short Buildup", "oiChange": -8765},
            ],
        }
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(return_value=_make_response(200, _LOGIN_SUCCESS_BODY))
        mock_client.request = AsyncMock(return_value=_make_response(200, oi_body))

        adapter = _make_adapter(mock_client)
        result = await adapter.fetch_oi_buildup()

        assert len(result) == 2
        assert result[0]["symbol"] == "RELIANCE"
        assert result[0]["provider"] == "angel_one"
        assert result[0]["sourceType"] == "BROKER_AUTHENTICATED"
        assert "fetchedAt" in result[0]

    @pytest.mark.asyncio
    async def test_fetch_oi_buildup_empty_list(self) -> None:
        """Empty OI buildup list returns an empty list without error."""
        body = {"status": True, "message": "SUCCESS", "data": []}
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(return_value=_make_response(200, _LOGIN_SUCCESS_BODY))
        mock_client.request = AsyncMock(return_value=_make_response(200, body))

        adapter = _make_adapter(mock_client)
        result = await adapter.fetch_oi_buildup()

        assert result == []


class TestFetchGainersLosers:
    """Test gainers/losers fetch."""

    @pytest.mark.asyncio
    async def test_fetch_gainers_losers_returns_data_with_metadata(self) -> None:
        """Gainers/losers response includes provider and sourceType metadata."""
        gl_body = {
            "status": True,
            "message": "SUCCESS",
            "data": {
                "gainers": [{"symbol": "INFY", "changePct": 2.5}],
                "losers": [{"symbol": "HDFC", "changePct": -1.8}],
            },
        }
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(return_value=_make_response(200, _LOGIN_SUCCESS_BODY))
        mock_client.request = AsyncMock(return_value=_make_response(200, gl_body))

        adapter = _make_adapter(mock_client)
        result = await adapter.fetch_gainers_losers()

        assert "gainers" in result
        assert "losers" in result
        assert result["provider"] == "angel_one"
        assert result["sourceType"] == "BROKER_AUTHENTICATED"
        assert "fetchedAt" in result


# ---------------------------------------------------------------------------
# _parse_retry_after utility
# ---------------------------------------------------------------------------


class TestParseRetryAfter:
    """Unit tests for the Retry-After header parser."""

    def test_valid_integer_header(self) -> None:
        """Integer Retry-After header returns the integer seconds value."""
        resp = _make_response(429, {}, headers={"Retry-After": "45"})
        assert _parse_retry_after(resp) == 45

    def test_missing_header_returns_none(self) -> None:
        """Missing Retry-After header returns None."""
        resp = _make_response(429, {})
        assert _parse_retry_after(resp) is None

    def test_non_integer_header_returns_none(self) -> None:
        """Non-integer Retry-After (e.g. HTTP-date) returns None."""
        resp = _make_response(429, {}, headers={"Retry-After": "Mon, 05 Feb 2024 10:00:00 GMT"})
        assert _parse_retry_after(resp) is None


# ---------------------------------------------------------------------------
# Interval map completeness
# ---------------------------------------------------------------------------


class TestIntervalMap:
    """Verify the interval map covers all required canonical intervals."""

    def test_all_angel_one_supported_intervals_are_mapped(self) -> None:
        """All intervals documented in the design are present in _INTERVAL_MAP."""
        required = {"1m", "5m", "10m", "15m", "30m", "1h", "1d", "1w"}
        for interval in required:
            assert interval in _INTERVAL_MAP, f"Interval {interval!r} missing from _INTERVAL_MAP"

    def test_3m_not_in_interval_map(self) -> None:
        """3m must never be in _INTERVAL_MAP (permanently banned for Indian markets)."""
        assert "3m" not in _INTERVAL_MAP


# ---------------------------------------------------------------------------
# Close / lifecycle
# ---------------------------------------------------------------------------


class TestAdapterLifecycle:
    """Test resource cleanup."""

    @pytest.mark.asyncio
    async def test_close_calls_aclose_when_adapter_owns_client(self) -> None:
        """When the adapter created the HTTP client, close() calls aclose()."""
        # Adapter without injected client — it will create one lazily
        adapter = _make_adapter(http_client=None)

        # Trigger client creation
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.aclose = AsyncMock()
        adapter._http_client = mock_client
        adapter._owns_client = True

        await adapter.close()

        mock_client.aclose.assert_called_once()

    @pytest.mark.asyncio
    async def test_close_does_not_call_aclose_when_client_injected(self) -> None:
        """When the HTTP client was injected, close() must NOT close it."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.aclose = AsyncMock()

        adapter = _make_adapter(http_client=mock_client)
        # _owns_client is False for injected clients
        await adapter.close()

        mock_client.aclose.assert_not_called()
