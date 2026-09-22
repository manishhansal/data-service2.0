"""
Unit tests for the Upstox V2/V3 provider adapter.

Coverage:
- Historical OHLCV fetch with correct interval mapping
- HTTP 401 triggers token refresh and single retry (success case)
- HTTP 401 triggers token refresh and retry failure → ProviderAuthError
- 3m interval raises ValueError before any I/O
- HTTP 429 raises ProviderRateLimitedError (no circuit-breaker failure)
- Live quote fetch
- Batch market quote fetch
- Credentials are never exposed in log entries

Requirements: 5.9, 19.8
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.providers.adapters.upstox import (
    INTERVAL_MAP,
    UPSTOX_BASE_URL,
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
    """Build a fake ``httpx.Response`` without a real HTTP connection."""
    json_body = json_body or {}
    headers = headers or {}
    request = httpx.Request("GET", "https://api.upstox.com/v2/test")
    return httpx.Response(
        status_code=status_code,
        json=json_body,
        headers=headers,
        request=request,
    )


def _candles_response(candles: list[Any]) -> dict[str, Any]:
    """Wrap a candle list in the Upstox API envelope."""
    return {"status": "success", "data": {"candles": candles}}


def _quote_response(instrument_key: str, quote: dict[str, Any]) -> dict[str, Any]:
    """Wrap a quote dict in the Upstox API envelope."""
    return {"status": "success", "data": {instrument_key: quote}}


def _make_adapter(mock_client: httpx.AsyncClient | None = None) -> UpstoxAdapter:
    """Return an UpstoxAdapter backed by a mock HTTP client."""
    client = mock_client or AsyncMock(spec=httpx.AsyncClient)
    adapter = UpstoxAdapter(
        api_key="test-api-key",
        api_secret="test-api-secret",
        redirect_uri="https://localhost/callback",
        http_client=client,
    )
    return adapter


# ---------------------------------------------------------------------------
# Interval mapping
# ---------------------------------------------------------------------------


class TestIntervalMapping:
    """Verify canonical interval → Upstox API interval translation."""

    @pytest.mark.parametrize(
        "canonical, expected_api",
        [
            ("1m",  "1minute"),
            ("5m",  "5minute"),
            ("10m", "10minute"),
            ("15m", "15minute"),
            ("30m", "30minute"),
            ("1h",  "60minute"),
            # Upstox V2 uses "day"/"week"/"month" — NOT "1day"/"1week"/"1month".
            # Verified 2026-09-14 against live Upstox V2 API.
            ("1d",  "day"),
            ("1w",  "week"),
            ("1M",  "month"),
        ],
    )
    def test_interval_map_canonical_to_api(
        self, canonical: str, expected_api: str
    ) -> None:
        assert INTERVAL_MAP[canonical] == expected_api

    def test_3m_absent_from_interval_map(self) -> None:
        """3m must not appear in the Upstox interval map for Indian market data."""
        assert "3m" not in INTERVAL_MAP


# ---------------------------------------------------------------------------
# 3m interval block
# ---------------------------------------------------------------------------


class Test3mIntervalBlock:
    """3m interval must be rejected before any I/O."""

    async def test_fetch_historical_ohlcv_raises_for_3m(self) -> None:
        adapter = _make_adapter()
        await adapter.set_access_token("valid-token")

        with pytest.raises(ValueError, match="3m interval unsupported"):
            await adapter.fetch_historical_ohlcv(
                instrument_key="NSE_EQ|INE002A01018",
                from_date="2024-01-01",
                to_date="2024-01-31",
                interval="3m",
            )

    async def test_3m_raises_before_any_http_call(self) -> None:
        """HTTP client must NOT be called when interval is 3m."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        adapter = _make_adapter(mock_client)
        await adapter.set_access_token("valid-token")

        with pytest.raises(ValueError):
            await adapter.fetch_historical_ohlcv(
                instrument_key="NSE_EQ|INE002A01018",
                from_date="2024-01-01",
                to_date="2024-01-31",
                interval="3m",
            )

        mock_client.request.assert_not_called()

    async def test_3m_error_message_mentions_requirement(self) -> None:
        adapter = _make_adapter()
        await adapter.set_access_token("valid-token")

        with pytest.raises(ValueError) as exc_info:
            await adapter.fetch_historical_ohlcv(
                "NSE_EQ|INE002A01018", "2024-01-01", "2024-01-31", "3m"
            )

        assert "3m" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Historical OHLCV — success path
# ---------------------------------------------------------------------------


class TestFetchHistoricalOHLCV:
    """Happy-path and shape tests for historical OHLCV fetching."""

    async def test_returns_candle_list_on_success(self) -> None:
        fake_candles = [
            ["2024-01-15T09:15:00+05:30", 500.0, 505.0, 498.0, 503.0, 100000, 0],
            ["2024-01-15T09:16:00+05:30", 503.0, 507.0, 502.0, 506.0, 80000, 0],
        ]
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.request.return_value = _mock_response(
            200, _candles_response(fake_candles)
        )
        adapter = _make_adapter(mock_client)
        await adapter.set_access_token("valid-token")

        result = await adapter.fetch_historical_ohlcv(
            "NSE_EQ|INE002A01018", "2024-01-15", "2024-01-15", "1m"
        )

        # V3 adapter returns normalized dicts with named keys, not raw arrays
        assert len(result) == 2
        assert result[0]["timestamp"] == "2024-01-15T09:15:00+05:30"
        assert result[0]["open"] == 500.0
        assert result[0]["close"] == 503.0
        assert result[0]["volume"] == 100000
        assert result[0]["open_interest"] == 0
        assert result[0]["provider"] == "upstox"
        assert result[0]["api_version"] == "v3"

    @pytest.mark.parametrize("interval", ["1m", "5m", "10m", "15m", "30m", "1h", "1d"])
    async def test_uses_correct_api_interval_in_url(self, interval: str) -> None:
        """The V3 unit/interval path must appear in the request URL."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.request.return_value = _mock_response(
            200, _candles_response([])
        )
        adapter = _make_adapter(mock_client)
        await adapter.set_access_token("valid-token")

        await adapter.fetch_historical_ohlcv(
            "NSE_EQ|INE002A01018", "2024-01-01", "2024-01-31", interval
        )

        call_args = mock_client.request.call_args
        called_url: str = call_args[0][1]  # positional: method, url
        # V3 URL contains unit/interval_value path segments
        from src.providers.adapters.upstox import INTERVAL_MAP_V3
        unit, interval_value = INTERVAL_MAP_V3[interval]
        assert unit in called_url, (
            f"Expected unit {unit!r} in URL for interval {interval!r}; got {called_url!r}"
        )
        assert str(interval_value) in called_url, (
            f"Expected interval_value {interval_value!r} in URL; got {called_url!r}"
        )
        # Must be V3 URL
        assert "/v3/historical-candle" in called_url

    async def test_request_uses_bearer_token(self) -> None:
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.request.return_value = _mock_response(
            200, _candles_response([])
        )
        adapter = _make_adapter(mock_client)
        await adapter.set_access_token("my-secret-token")

        await adapter.fetch_historical_ohlcv(
            "NSE_EQ|INE002A01018", "2024-01-01", "2024-01-31", "1d"
        )

        call_args = mock_client.request.call_args
        headers: dict[str, str] = call_args.kwargs.get("headers", {})
        assert headers.get("Authorization") == "Bearer my-secret-token"

    async def test_empty_candles_returns_empty_list(self) -> None:
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.request.return_value = _mock_response(
            200, _candles_response([])
        )
        adapter = _make_adapter(mock_client)
        await adapter.set_access_token("valid-token")

        result = await adapter.fetch_historical_ohlcv(
            "NSE_EQ|INE002A01018", "2024-01-01", "2024-01-31", "1d"
        )
        assert result == []

    async def test_unexpected_response_shape_returns_empty_list(self) -> None:
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.request.return_value = _mock_response(
            200, {"status": "success"}  # missing "data" key
        )
        adapter = _make_adapter(mock_client)
        await adapter.set_access_token("valid-token")

        result = await adapter.fetch_historical_ohlcv(
            "NSE_EQ|INE002A01018", "2024-01-01", "2024-01-31", "1d"
        )
        assert result == []

    async def test_unsupported_interval_raises_value_error(self) -> None:
        adapter = _make_adapter()
        await adapter.set_access_token("valid-token")

        with pytest.raises(ValueError, match="Unsupported interval"):
            await adapter.fetch_historical_ohlcv(
                "NSE_EQ|INE002A01018", "2024-01-01", "2024-01-31", "99x"
            )


# ---------------------------------------------------------------------------
# 401 handling — success case (refresh + retry succeeds)
# ---------------------------------------------------------------------------


class TestHttp401SuccessCase:
    """HTTP 401 → token refresh → retry succeeds (Requirement 19.8)."""

    async def test_401_triggers_token_refresh_and_retry(self) -> None:
        """On 401 the adapter refreshes the token and retries once."""
        fake_candles = [
            ["2024-01-15T09:15:00+05:30", 500.0, 505.0, 498.0, 503.0, 100000, 0],
        ]
        mock_client = AsyncMock(spec=httpx.AsyncClient)

        token_refresh_response = _mock_response(
            200, {"access_token": "refreshed-token"}
        )
        ok_response = _mock_response(200, _candles_response(fake_candles))

        mock_client.request.side_effect = [
            _mock_response(401, {}),  # initial request → 401
            ok_response,              # retry after refresh → 200
        ]
        mock_client.post.return_value = token_refresh_response

        adapter = _make_adapter(mock_client)
        await adapter.set_access_token("expired-token")

        result = await adapter.fetch_historical_ohlcv(
            "NSE_EQ|INE002A01018", "2024-01-15", "2024-01-15", "1m"
        )

        # V3 returns normalized dicts
        assert len(result) == 1
        assert result[0]["open"] == 500.0
        assert result[0]["close"] == 503.0
        # Exactly two request() calls were made.
        assert mock_client.request.call_count == 2
        # Token refresh (POST) was called exactly once.
        mock_client.post.assert_called_once()

    async def test_token_updated_after_successful_refresh(self) -> None:
        """After a successful 401-triggered refresh the new token is used."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.request.side_effect = [
            _mock_response(401, {}),
            _mock_response(200, _candles_response([])),
        ]
        mock_client.post.return_value = _mock_response(
            200, {"access_token": "brand-new-token"}
        )

        adapter = _make_adapter(mock_client)
        await adapter.set_access_token("old-token")

        await adapter.fetch_historical_ohlcv(
            "NSE_EQ|INE002A01018", "2024-01-01", "2024-01-31", "1d"
        )

        # The retry request must use the new token.
        second_call = mock_client.request.call_args_list[1]
        headers: dict[str, str] = second_call.kwargs.get("headers", {})
        assert headers.get("Authorization") == "Bearer brand-new-token"


# ---------------------------------------------------------------------------
# 401 handling — failure case (refresh itself fails → ProviderAuthError)
# ---------------------------------------------------------------------------


class TestHttp401FailureCase:
    """HTTP 401 + failed token refresh → ProviderAuthError (Requirement 19.8)."""

    async def test_401_with_failed_refresh_raises_provider_auth_error(self) -> None:
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.request.return_value = _mock_response(401, {})
        # Token refresh POST returns 400 Bad Request (invalid credentials).
        mock_client.post.return_value = _mock_response(400, {"error": "invalid_client"})

        adapter = _make_adapter(mock_client)
        await adapter.set_access_token("expired-token")

        with pytest.raises(ProviderAuthError):
            await adapter.fetch_historical_ohlcv(
                "NSE_EQ|INE002A01018", "2024-01-01", "2024-01-31", "1d"
            )

    async def test_401_with_network_error_on_refresh_raises_provider_auth_error(
        self,
    ) -> None:
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.request.return_value = _mock_response(401, {})
        mock_client.post.side_effect = httpx.ConnectError("connection refused")

        adapter = _make_adapter(mock_client)
        await adapter.set_access_token("expired-token")

        with pytest.raises(ProviderAuthError):
            await adapter.fetch_historical_ohlcv(
                "NSE_EQ|INE002A01018", "2024-01-01", "2024-01-31", "1d"
            )

    async def test_double_401_raises_provider_auth_error(self) -> None:
        """If the retry also returns 401, ProviderAuthError must be raised."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        # Both the initial request and the retry return 401.
        mock_client.request.side_effect = [
            _mock_response(401, {}),
            _mock_response(401, {}),
        ]
        # Token refresh succeeds (returns a new token), but the API still 401s.
        mock_client.post.return_value = _mock_response(
            200, {"access_token": "another-expired-token"}
        )

        adapter = _make_adapter(mock_client)
        await adapter.set_access_token("expired-token")

        with pytest.raises(ProviderAuthError):
            await adapter.fetch_historical_ohlcv(
                "NSE_EQ|INE002A01018", "2024-01-01", "2024-01-31", "1d"
            )

    async def test_provider_auth_error_has_upstox_provider(self) -> None:
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.request.return_value = _mock_response(401, {})
        mock_client.post.return_value = _mock_response(500, {})

        adapter = _make_adapter(mock_client)
        await adapter.set_access_token("expired-token")

        with pytest.raises(ProviderAuthError) as exc_info:
            await adapter.fetch_historical_ohlcv(
                "NSE_EQ|INE002A01018", "2024-01-01", "2024-01-31", "1d"
            )

        assert exc_info.value.provider == "upstox"

    async def test_no_token_set_raises_provider_auth_error(self) -> None:
        """ensure_authenticated raises ProviderAuthError when no token is set."""
        adapter = _make_adapter()
        # Do NOT call set_access_token — adapter has no token.

        with pytest.raises(ProviderAuthError, match="No Upstox"):
            await adapter.ensure_authenticated()


# ---------------------------------------------------------------------------
# Rate limit (HTTP 429)
# ---------------------------------------------------------------------------


class TestRateLimit:
    """HTTP 429 must raise ProviderRateLimitedError (not a circuit-breaker failure)."""

    async def test_429_raises_provider_rate_limited_error(self) -> None:
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.request.return_value = _mock_response(
            429, {}, headers={"Retry-After": "60"}
        )
        adapter = _make_adapter(mock_client)
        await adapter.set_access_token("valid-token")

        with pytest.raises(ProviderRateLimitedError):
            await adapter.fetch_historical_ohlcv(
                "NSE_EQ|INE002A01018", "2024-01-01", "2024-01-31", "1d"
            )

    async def test_429_parses_retry_after_header(self) -> None:
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.request.return_value = _mock_response(
            429, {}, headers={"Retry-After": "45"}
        )
        adapter = _make_adapter(mock_client)
        await adapter.set_access_token("valid-token")

        with pytest.raises(ProviderRateLimitedError) as exc_info:
            await adapter.fetch_historical_ohlcv(
                "NSE_EQ|INE002A01018", "2024-01-01", "2024-01-31", "1d"
            )

        assert exc_info.value.retry_after_sec == 45

    async def test_429_without_retry_after_header(self) -> None:
        """Absent Retry-After header → retry_after_sec is None."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.request.return_value = _mock_response(429, {})
        adapter = _make_adapter(mock_client)
        await adapter.set_access_token("valid-token")

        with pytest.raises(ProviderRateLimitedError) as exc_info:
            await adapter.fetch_historical_ohlcv(
                "NSE_EQ|INE002A01018", "2024-01-01", "2024-01-31", "1d"
            )

        assert exc_info.value.retry_after_sec is None

    async def test_429_raises_for_live_quote_too(self) -> None:
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.request.return_value = _mock_response(429, {})
        adapter = _make_adapter(mock_client)
        await adapter.set_access_token("valid-token")

        with pytest.raises(ProviderRateLimitedError):
            await adapter.fetch_live_quote("NSE_EQ|INE002A01018")

    async def test_rate_limited_error_has_upstox_provider(self) -> None:
        exc = ProviderRateLimitedError(retry_after_sec=30)
        assert exc.provider == "upstox"


# ---------------------------------------------------------------------------
# Live quote
# ---------------------------------------------------------------------------


class TestFetchLiveQuote:
    """Live quote fetching returns the correct instrument's data."""

    async def test_returns_quote_for_instrument(self) -> None:
        instrument_key = "NSE_EQ|INE002A01018"
        fake_quote = {
            "last_price": 2450.5,
            "volume": 1_500_000,
            "open_price": 2430.0,
            "high_price": 2460.0,
            "low_price": 2425.0,
        }
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.request.return_value = _mock_response(
            200, _quote_response(instrument_key, fake_quote)
        )
        adapter = _make_adapter(mock_client)
        await adapter.set_access_token("valid-token")

        result = await adapter.fetch_live_quote(instrument_key)

        assert result == fake_quote

    async def test_live_quote_uses_correct_url(self) -> None:
        instrument_key = "NSE_EQ|INE002A01018"
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.request.return_value = _mock_response(
            200, _quote_response(instrument_key, {})
        )
        adapter = _make_adapter(mock_client)
        await adapter.set_access_token("valid-token")

        await adapter.fetch_live_quote(instrument_key)

        call_args = mock_client.request.call_args
        called_url: str = call_args[0][1]
        # V3 endpoint must be used (migrated April 2025)
        assert "v3/market-quote/quotes" in called_url

    async def test_live_quote_passes_instrument_key_as_param(self) -> None:
        instrument_key = "NSE_IDX|Nifty 50"
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.request.return_value = _mock_response(
            200, _quote_response(instrument_key, {"last_price": 22500.0})
        )
        adapter = _make_adapter(mock_client)
        await adapter.set_access_token("valid-token")

        await adapter.fetch_live_quote(instrument_key)

        call_args = mock_client.request.call_args
        params: dict[str, str] = call_args.kwargs.get("params", {})
        assert params.get("instrument_key") == instrument_key

    async def test_live_quote_missing_key_raises_key_error(self) -> None:
        """If the instrument key is absent from the response, KeyError is raised."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.request.return_value = _mock_response(
            200, {"status": "success", "data": {}}  # empty data
        )
        adapter = _make_adapter(mock_client)
        await adapter.set_access_token("valid-token")

        with pytest.raises(KeyError):
            await adapter.fetch_live_quote("NSE_EQ|INE002A01018")


# ---------------------------------------------------------------------------
# Batch market quote
# ---------------------------------------------------------------------------


class TestFetchMarketQuote:
    """Batch market quote handling."""

    async def test_returns_dict_keyed_by_instrument(self) -> None:
        keys = ["NSE_EQ|INE002A01018", "NSE_EQ|INE040A01034"]
        data = {k: {"last_price": float(i + 100)} for i, k in enumerate(keys)}
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.request.return_value = _mock_response(
            200, {"status": "success", "data": data}
        )
        adapter = _make_adapter(mock_client)
        await adapter.set_access_token("valid-token")

        result = await adapter.fetch_market_quote(keys)

        assert set(result.keys()) == set(keys)

    async def test_empty_keys_raises_value_error(self) -> None:
        adapter = _make_adapter()
        await adapter.set_access_token("valid-token")

        with pytest.raises(ValueError, match="instrument_keys must not be empty"):
            await adapter.fetch_market_quote([])

    async def test_keys_joined_as_comma_separated_param(self) -> None:
        keys = ["NSE_EQ|INE002A01018", "NSE_EQ|INE040A01034"]
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.request.return_value = _mock_response(
            200, {"status": "success", "data": {}}
        )
        adapter = _make_adapter(mock_client)
        await adapter.set_access_token("valid-token")

        await adapter.fetch_market_quote(keys)

        call_args = mock_client.request.call_args
        params: dict[str, str] = call_args.kwargs.get("params", {})
        # Both keys must appear in the query parameter value.
        param_value: str = params.get("instrument_key", "")
        for k in keys:
            assert k in param_value


# ---------------------------------------------------------------------------
# Credential safety — tokens must never appear in log output
# ---------------------------------------------------------------------------


class TestCredentialSafety:
    """Verify that credentials are never written to log entries."""

    async def test_access_token_not_in_log_entries(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The Bearer token value must never appear in any log record."""
        secret_token = "SUPER_SECRET_TOKEN_DO_NOT_LOG_ABC123"

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.request.return_value = _mock_response(
            200, _candles_response([])
        )
        adapter = _make_adapter(mock_client)
        await adapter.set_access_token(secret_token)

        with caplog.at_level(logging.DEBUG):
            await adapter.fetch_historical_ohlcv(
                "NSE_EQ|INE002A01018", "2024-01-01", "2024-01-31", "1d"
            )

        # Check all log messages — the secret token must not appear in any.
        all_log_text = " ".join(record.message for record in caplog.records)
        assert secret_token not in all_log_text, (
            "Secret access token was found in log output — credential leak!"
        )

    async def test_api_key_not_in_log_entries(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The API key must never appear in any log record during a 401 refresh."""
        secret_key = "SECRET_API_KEY_MUST_NOT_APPEAR_IN_LOGS"
        secret_secret = "SECRET_API_SECRET_MUST_NOT_APPEAR_IN_LOGS"

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.request.return_value = _mock_response(401, {})
        mock_client.post.return_value = _mock_response(400, {"error": "bad"})

        adapter = UpstoxAdapter(
            api_key=secret_key,
            api_secret=secret_secret,
            redirect_uri="https://localhost/callback",
            http_client=mock_client,
        )
        await adapter.set_access_token("token")

        with caplog.at_level(logging.DEBUG):
            with pytest.raises(ProviderAuthError):
                await adapter.fetch_historical_ohlcv(
                    "NSE_EQ|INE002A01018", "2024-01-01", "2024-01-31", "1d"
                )

        all_log_text = " ".join(record.message for record in caplog.records)
        assert secret_key not in all_log_text, "API key leaked into logs!"
        assert secret_secret not in all_log_text, "API secret leaked into logs!"

    async def test_refreshed_token_not_in_log_entries(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A newly-refreshed token value must not appear in logs."""
        refreshed_token = "REFRESHED_SECRET_TOKEN_XYZ987"

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.request.side_effect = [
            _mock_response(401, {}),
            _mock_response(200, _candles_response([])),
        ]
        mock_client.post.return_value = _mock_response(
            200, {"access_token": refreshed_token}
        )

        adapter = _make_adapter(mock_client)
        await adapter.set_access_token("old-token")

        with caplog.at_level(logging.DEBUG):
            await adapter.fetch_historical_ohlcv(
                "NSE_EQ|INE002A01018", "2024-01-01", "2024-01-31", "1d"
            )

        all_log_text = " ".join(record.message for record in caplog.records)
        assert refreshed_token not in all_log_text, "Refreshed token leaked into logs!"


# ---------------------------------------------------------------------------
# Token management
# ---------------------------------------------------------------------------


class TestTokenManagement:
    """set_access_token, refresh_token, ensure_authenticated."""

    async def test_set_access_token_stores_token(self) -> None:
        adapter = _make_adapter()
        await adapter.set_access_token("my-token")
        assert adapter._access_token == "my-token"

    async def test_set_access_token_overwrites_previous(self) -> None:
        adapter = _make_adapter()
        await adapter.set_access_token("first-token")
        await adapter.set_access_token("second-token")
        assert adapter._access_token == "second-token"

    async def test_refresh_token_updates_access_token(self) -> None:
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.return_value = _mock_response(
            200, {"access_token": "fresh-token"}
        )
        adapter = _make_adapter(mock_client)
        await adapter.set_access_token("old-token")

        await adapter.refresh_token()

        assert adapter._access_token == "fresh-token"

    async def test_refresh_token_raises_on_missing_access_token_field(
        self,
    ) -> None:
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.return_value = _mock_response(200, {"other_field": "value"})
        adapter = _make_adapter(mock_client)

        with pytest.raises(ProviderAuthError, match="missing 'access_token'"):
            await adapter.refresh_token()

    async def test_ensure_authenticated_passes_when_token_set(self) -> None:
        adapter = _make_adapter()
        await adapter.set_access_token("valid-token")
        # Should not raise.
        await adapter.ensure_authenticated()

    async def test_ensure_authenticated_raises_without_token(self) -> None:
        adapter = _make_adapter()
        with pytest.raises(ProviderAuthError):
            await adapter.ensure_authenticated()


# ---------------------------------------------------------------------------
# Lifecycle / context manager
# ---------------------------------------------------------------------------


class TestLifecycle:
    """Context manager and aclose() behaviour."""

    async def test_aclose_closes_owned_client(self) -> None:
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        adapter = UpstoxAdapter(
            api_key="key", api_secret="secret",
            http_client=mock_client,
        )
        # When the client is provided externally, _owned_client is False.
        adapter._owned_client = True
        await adapter.aclose()
        mock_client.aclose.assert_called_once()

    async def test_aclose_does_not_close_external_client(self) -> None:
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        # Provide an external client — _owned_client remains False.
        adapter = UpstoxAdapter(
            api_key="key", api_secret="secret",
            http_client=mock_client,
        )
        assert adapter._owned_client is False
        await adapter.aclose()
        mock_client.aclose.assert_not_called()

    async def test_context_manager_calls_aclose(self) -> None:
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        adapter = UpstoxAdapter(
            api_key="key", api_secret="secret",
            http_client=mock_client,
        )
        adapter._owned_client = True

        async with adapter:
            pass

        mock_client.aclose.assert_called_once()


# ---------------------------------------------------------------------------
# Market Information APIs (launched May 2026)
# ---------------------------------------------------------------------------


class TestMarketInformationApis:
    """Verify Market Information endpoints call correct V2 URLs."""

    async def test_fetch_oi_data_url_and_params(self) -> None:
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.request.return_value = _mock_response(
            200, {"status": "success", "data": {"strikes": []}}
        )
        adapter = _make_adapter(mock_client)
        await adapter.set_access_token("tok")

        result = await adapter.fetch_oi_data(
            "NSE_INDEX|Nifty 50", "2026-09-29", "2026-09-17"
        )

        call_args = mock_client.request.call_args
        called_url: str = call_args[0][1]
        assert "v2/market/oi" in called_url
        params: dict = call_args.kwargs.get("params", {})
        assert params["instrument_key"] == "NSE_INDEX|Nifty 50"
        assert params["expiry"] == "2026-09-29"
        assert params["date"] == "2026-09-17"
        assert isinstance(result, dict)

    async def test_fetch_pcr_data_url_and_params(self) -> None:
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.request.return_value = _mock_response(
            200, {"status": "success", "data": {"pcr_series": []}}
        )
        adapter = _make_adapter(mock_client)
        await adapter.set_access_token("tok")

        result = await adapter.fetch_pcr_data(
            "NSE_INDEX|Nifty 50", "2026-09-29", "2026-09-17", 30
        )

        call_args = mock_client.request.call_args
        called_url: str = call_args[0][1]
        assert "v2/market/pcr" in called_url
        params: dict = call_args.kwargs.get("params", {})
        assert params["bucket_interval"] == 30
        assert isinstance(result, dict)

    async def test_fetch_max_pain_url_and_params(self) -> None:
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.request.return_value = _mock_response(
            200, {"status": "success", "data": {"max_pain_strike": 23000}}
        )
        adapter = _make_adapter(mock_client)
        await adapter.set_access_token("tok")

        result = await adapter.fetch_max_pain(
            "NSE_INDEX|Nifty 50", "2026-09-29", "2026-09-17", 15
        )

        call_args = mock_client.request.call_args
        called_url: str = call_args[0][1]
        assert "v2/market/max-pain" in called_url
        params: dict = call_args.kwargs.get("params", {})
        assert params["bucket_interval"] == 15
        assert isinstance(result, dict)

    async def test_fetch_change_oi_url_and_params(self) -> None:
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.request.return_value = _mock_response(
            200, {"status": "success", "data": {"change_oi": []}}
        )
        adapter = _make_adapter(mock_client)
        await adapter.set_access_token("tok")

        result = await adapter.fetch_change_oi(
            "NSE_INDEX|Nifty 50", "2026-09-29", "2026-09-17", 1
        )

        call_args = mock_client.request.call_args
        called_url: str = call_args[0][1]
        assert "v2/market/change-oi" in called_url
        params: dict = call_args.kwargs.get("params", {})
        assert params["interval"] == 1
        assert isinstance(result, dict)

    async def test_fetch_fii_data_url_and_required_params(self) -> None:
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.request.return_value = _mock_response(
            200, {"status": "success", "data": {"fii": []}}
        )
        adapter = _make_adapter(mock_client)
        await adapter.set_access_token("tok")

        result = await adapter.fetch_fii_data("NSE_FO|INDEX_FUTURES", "1D")

        call_args = mock_client.request.call_args
        called_url: str = call_args[0][1]
        assert "v2/market/fii" in called_url
        params: dict = call_args.kwargs.get("params", {})
        assert params["data_type"] == "NSE_FO|INDEX_FUTURES"
        assert params["interval"] == "1D"
        assert "from" not in params  # from_date not passed
        assert isinstance(result, dict)

    async def test_fetch_fii_data_optional_from_date(self) -> None:
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.request.return_value = _mock_response(
            200, {"status": "success", "data": {}}
        )
        adapter = _make_adapter(mock_client)
        await adapter.set_access_token("tok")

        await adapter.fetch_fii_data("NSE_EQ|CASH", "1M", from_date="2026-01-01")

        params: dict = mock_client.request.call_args.kwargs.get("params", {})
        assert params["from"] == "2026-01-01"

    async def test_fetch_dii_data_url_and_required_params(self) -> None:
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.request.return_value = _mock_response(
            200, {"status": "success", "data": {"dii": []}}
        )
        adapter = _make_adapter(mock_client)
        await adapter.set_access_token("tok")

        result = await adapter.fetch_dii_data("NSE_EQ|CASH", "1D")

        call_args = mock_client.request.call_args
        called_url: str = call_args[0][1]
        assert "v2/market/dii" in called_url
        params: dict = call_args.kwargs.get("params", {})
        assert params["data_type"] == "NSE_EQ|CASH"
        assert params["interval"] == "1D"
        assert isinstance(result, dict)

    async def test_market_info_empty_data_returns_empty_dict(self) -> None:
        """Provider returning null/missing data → empty dict, no exception."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.request.return_value = _mock_response(
            200, {"status": "success", "data": None}
        )
        adapter = _make_adapter(mock_client)
        await adapter.set_access_token("tok")

        result = await adapter.fetch_oi_data("key", "2026-09-29", "2026-09-17")
        assert result == {}


# ---------------------------------------------------------------------------
# Smartlist APIs (launched May 2026)
# ---------------------------------------------------------------------------


class TestSmartlistApis:
    """Verify Smartlist endpoints call correct V2 URLs with optional params."""

    async def test_fetch_smartlist_futures_no_params(self) -> None:
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.request.return_value = _mock_response(
            200, {"status": "success", "data": {"items": []}}
        )
        adapter = _make_adapter(mock_client)
        await adapter.set_access_token("tok")

        result = await adapter.fetch_smartlist_futures()

        call_args = mock_client.request.call_args
        called_url: str = call_args[0][1]
        assert "v2/market/smartlist/futures" in called_url
        params: dict = call_args.kwargs.get("params", {})
        # No optional params should be present
        assert "asset_type" not in params
        assert "category" not in params
        assert isinstance(result, dict)

    async def test_fetch_smartlist_futures_with_all_params(self) -> None:
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.request.return_value = _mock_response(
            200, {"status": "success", "data": {}}
        )
        adapter = _make_adapter(mock_client)
        await adapter.set_access_token("tok")

        await adapter.fetch_smartlist_futures(
            asset_type="INDEX", category="OI_GAINERS",
            page_number=1, page_size=20
        )

        params: dict = mock_client.request.call_args.kwargs.get("params", {})
        assert params["asset_type"] == "INDEX"
        assert params["category"] == "OI_GAINERS"
        assert params["page_number"] == 1
        assert params["page_size"] == 20

    async def test_fetch_smartlist_options_url(self) -> None:
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.request.return_value = _mock_response(
            200, {"status": "success", "data": {}}
        )
        adapter = _make_adapter(mock_client)
        await adapter.set_access_token("tok")

        await adapter.fetch_smartlist_options(asset_type="STOCK", category="IV_GAINERS")

        called_url: str = mock_client.request.call_args[0][1]
        assert "v2/market/smartlist/options" in called_url
        params: dict = mock_client.request.call_args.kwargs.get("params", {})
        assert params["asset_type"] == "STOCK"
        assert params["category"] == "IV_GAINERS"

    async def test_fetch_smartlist_mtf_url(self) -> None:
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.request.return_value = _mock_response(
            200, {"status": "success", "data": {"items": []}}
        )
        adapter = _make_adapter(mock_client)
        await adapter.set_access_token("tok")

        result = await adapter.fetch_smartlist_mtf(page_number=2, page_size=50)

        called_url: str = mock_client.request.call_args[0][1]
        assert "v2/market/smartlist/mtf" in called_url
        params: dict = mock_client.request.call_args.kwargs.get("params", {})
        assert params["page_number"] == 2
        assert params["page_size"] == 50
        assert isinstance(result, dict)

    async def test_smartlist_empty_data_returns_empty_dict(self) -> None:
        """Provider returning null data field → empty dict, no exception."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.request.return_value = _mock_response(
            200, {"status": "success", "data": None}
        )
        adapter = _make_adapter(mock_client)
        await adapter.set_access_token("tok")

        result = await adapter.fetch_smartlist_futures()
        assert result == {}


# ---------------------------------------------------------------------------
# Full quote V3 migration verification
# ---------------------------------------------------------------------------


class TestFullQuoteV3Migration:
    """Verify fetch_full_quote now calls the V3 endpoint."""

    async def test_full_quote_uses_v3_url(self) -> None:
        """fetch_full_quote must call /v3/market-quote/quotes, not V2."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.request.return_value = _mock_response(
            200, {"status": "success", "data": {}}
        )
        adapter = _make_adapter(mock_client)
        await adapter.set_access_token("valid-token")

        await adapter.fetch_full_quote(["NSE_EQ|INE002A01018"])

        called_url: str = mock_client.request.call_args[0][1]
        assert "v3/market-quote/quotes" in called_url
        assert "v2/market-quote/quotes" not in called_url

    async def test_full_quote_v3_returns_data(self) -> None:
        instrument_key = "NSE_EQ|INE002A01018"
        fake_quote = {
            "last_price": 1244.5,
            "volume": 1_200_000,
            "oi": 0.0,
        }
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.request.return_value = _mock_response(
            200, {"status": "success", "data": {instrument_key: fake_quote}}
        )
        adapter = _make_adapter(mock_client)
        await adapter.set_access_token("valid-token")

        result = await adapter.fetch_full_quote([instrument_key])

        assert result[instrument_key] == fake_quote

    async def test_full_quote_v3_accepts_cas_fields(self) -> None:
        """V3 full quote response with CAS fields must pass through unchanged."""
        instrument_key = "NSE_EQ|INE002A01018"
        fake_quote = {
            "last_price": 1244.5,
            "cas": {
                "iep": 1243.0,
                "ieq": "5000",
                "iiq_total": "200",
                "iiq_m": "50",
                "rp": "1244.0",
                "cas_eligible": True,
            },
        }
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.request.return_value = _mock_response(
            200, {"status": "success", "data": {instrument_key: fake_quote}}
        )
        adapter = _make_adapter(mock_client)
        await adapter.set_access_token("valid-token")

        result = await adapter.fetch_full_quote([instrument_key])

        # CAS fields must not be stripped
        assert result[instrument_key]["cas"]["iep"] == 1243.0
        assert result[instrument_key]["cas"]["cas_eligible"] is True
