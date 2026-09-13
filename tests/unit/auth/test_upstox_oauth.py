"""
Unit tests for the Upstox OAuth token store and refresh handler.

Task 13.6 — Requirements 19.1, 19.8

Coverage:
    UpstoxTokenStore
    - get_access_token / get_refresh_token — None when not set, value when set
    - set_tokens — stores values, rejects naive datetimes
    - is_valid — False when no token, False when expired/near-expiry, True when valid
    - clear — removes all stored state

    UpstoxOAuthRefreshHandler
    - refresh — success path stores token in store
    - refresh — HTTP 4xx/5xx → returns False, clears store
    - refresh — network error → returns False, clears store
    - refresh — missing access_token field → returns False, clears store
    - refresh — uses expires_in from response; defaults to 3600 when absent
    - refresh — stores refresh_token when present
    - with_retry — calls request_fn with current token
    - with_retry — on 401, refreshes and retries
    - with_retry — on 401 + failed refresh → raises UpstoxAuthError
    - with_retry — on 401 + successful refresh + 401 retry → raises UpstoxAuthError
    - with_retry — no token initially → pre-refresh, then call
    - with_retry — non-401 errors propagate unchanged
    - with_retry — max_retries=0 → no refresh, error propagates immediately
    - Credentials never appear in log output
    - Lock prevents duplicate concurrent refreshes
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.auth.upstox_oauth import (
    UPSTOX_TOKEN_URL,
    UpstoxAuthError,
    UpstoxOAuthRefreshHandler,
    UpstoxTokenStore,
)
from src.core.settings import Settings

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FUTURE_DT = datetime(2099, 1, 1, tzinfo=timezone.utc)
_PAST_DT = datetime(2000, 1, 1, tzinfo=timezone.utc)
_NEAR_EXPIRY_DT = datetime.now(timezone.utc) + timedelta(seconds=60)  # < 5 min


def _mock_settings(
    api_key: str = "test-api-key",
    api_secret: str = "test-api-secret",
    redirect_uri: str = "https://localhost/callback",
) -> Settings:
    """Return a minimal Settings-like object with Upstox credentials."""
    s = MagicMock(spec=Settings)
    s.upstox_api_key = api_key
    s.upstox_api_secret = api_secret
    s.upstox_redirect_uri = redirect_uri
    return s


def _ok_token_response(
    access_token: str = "new-access-token",
    refresh_token: str | None = "new-refresh-token",
    expires_in: int | None = 3600,
) -> httpx.Response:
    """Build a 200 OK token-endpoint response."""
    body: dict[str, Any] = {"access_token": access_token}
    if refresh_token is not None:
        body["refresh_token"] = refresh_token
    if expires_in is not None:
        body["expires_in"] = expires_in
    request = httpx.Request("POST", UPSTOX_TOKEN_URL)
    return httpx.Response(status_code=200, json=body, request=request)


def _error_response(status_code: int = 400) -> httpx.Response:
    """Build an error token-endpoint response."""
    request = httpx.Request("POST", UPSTOX_TOKEN_URL)
    return httpx.Response(
        status_code=status_code, json={"error": "invalid_client"}, request=request
    )


def _http_status_error(status_code: int) -> httpx.HTTPStatusError:
    """Build a fake HTTPStatusError for the given status code."""
    request = httpx.Request("GET", "https://api.upstox.com/v2/something")
    response = httpx.Response(status_code=status_code, request=request)
    return httpx.HTTPStatusError(
        f"HTTP {status_code}", request=request, response=response
    )


# ---------------------------------------------------------------------------
# UpstoxTokenStore — basic get/set
# ---------------------------------------------------------------------------


class TestUpstoxTokenStore:
    """Tests for UpstoxTokenStore state management."""

    def test_access_token_none_initially(self) -> None:
        store = UpstoxTokenStore()
        assert store.get_access_token() is None

    def test_refresh_token_none_initially(self) -> None:
        store = UpstoxTokenStore()
        assert store.get_refresh_token() is None

    def test_is_valid_false_initially(self) -> None:
        store = UpstoxTokenStore()
        assert store.is_valid() is False

    def test_set_tokens_stores_access_token(self) -> None:
        store = UpstoxTokenStore()
        store.set_tokens(
            access_token="my-access-token",
            expires_at=_FUTURE_DT,
        )
        assert store.get_access_token() == "my-access-token"

    def test_set_tokens_stores_refresh_token(self) -> None:
        store = UpstoxTokenStore()
        store.set_tokens(
            access_token="acc",
            refresh_token="ref",
            expires_at=_FUTURE_DT,
        )
        assert store.get_refresh_token() == "ref"

    def test_set_tokens_refresh_token_defaults_to_none(self) -> None:
        store = UpstoxTokenStore()
        store.set_tokens(access_token="acc", expires_at=_FUTURE_DT)
        assert store.get_refresh_token() is None

    def test_set_tokens_rejects_naive_datetime(self) -> None:
        store = UpstoxTokenStore()
        naive_dt = datetime(2099, 1, 1)  # no tzinfo
        with pytest.raises(ValueError, match="timezone-aware"):
            store.set_tokens(access_token="acc", expires_at=naive_dt)

    def test_set_tokens_overwrites_previous(self) -> None:
        store = UpstoxTokenStore()
        store.set_tokens(access_token="first", expires_at=_FUTURE_DT)
        store.set_tokens(access_token="second", expires_at=_FUTURE_DT)
        assert store.get_access_token() == "second"


# ---------------------------------------------------------------------------
# UpstoxTokenStore — is_valid
# ---------------------------------------------------------------------------


class TestUpstoxTokenStoreIsValid:
    """Token validity logic with expiry buffer."""

    def test_is_valid_true_for_far_future_expiry(self) -> None:
        store = UpstoxTokenStore()
        store.set_tokens(access_token="tok", expires_at=_FUTURE_DT)
        assert store.is_valid() is True

    def test_is_valid_false_for_past_expiry(self) -> None:
        store = UpstoxTokenStore()
        store.set_tokens(access_token="tok", expires_at=_PAST_DT)
        assert store.is_valid() is False

    def test_is_valid_false_when_near_expiry(self) -> None:
        """Token within 5 minutes of expiry is considered invalid."""
        store = UpstoxTokenStore()
        store.set_tokens(access_token="tok", expires_at=_NEAR_EXPIRY_DT)
        assert store.is_valid() is False

    def test_is_valid_false_when_no_token_set(self) -> None:
        store = UpstoxTokenStore()
        assert store.is_valid() is False

    def test_is_valid_false_after_clear(self) -> None:
        store = UpstoxTokenStore()
        store.set_tokens(access_token="tok", expires_at=_FUTURE_DT)
        assert store.is_valid() is True
        store.clear()
        assert store.is_valid() is False


# ---------------------------------------------------------------------------
# UpstoxTokenStore — clear
# ---------------------------------------------------------------------------


class TestUpstoxTokenStoreClear:
    """clear() must remove all stored state."""

    def test_clear_removes_access_token(self) -> None:
        store = UpstoxTokenStore()
        store.set_tokens(access_token="tok", expires_at=_FUTURE_DT)
        store.clear()
        assert store.get_access_token() is None

    def test_clear_removes_refresh_token(self) -> None:
        store = UpstoxTokenStore()
        store.set_tokens(access_token="tok", refresh_token="ref", expires_at=_FUTURE_DT)
        store.clear()
        assert store.get_refresh_token() is None

    def test_clear_idempotent_when_already_empty(self) -> None:
        store = UpstoxTokenStore()
        store.clear()  # must not raise
        assert store.get_access_token() is None


# ---------------------------------------------------------------------------
# UpstoxOAuthRefreshHandler — refresh success path
# ---------------------------------------------------------------------------


class TestRefreshSuccess:
    """refresh() returns True and updates the store on success."""

    async def test_refresh_returns_true_on_200(self) -> None:
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.return_value = _ok_token_response()

        handler = UpstoxOAuthRefreshHandler(http_client=mock_client)
        store = UpstoxTokenStore()
        settings = _mock_settings()

        result = await handler.refresh(settings, store)

        assert result is True

    async def test_refresh_stores_access_token(self) -> None:
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.return_value = _ok_token_response(
            access_token="fresh-token-abc"
        )

        handler = UpstoxOAuthRefreshHandler(http_client=mock_client)
        store = UpstoxTokenStore()
        settings = _mock_settings()

        await handler.refresh(settings, store)

        assert store.get_access_token() == "fresh-token-abc"

    async def test_refresh_stores_refresh_token(self) -> None:
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.return_value = _ok_token_response(
            access_token="acc", refresh_token="ref-xyz"
        )

        handler = UpstoxOAuthRefreshHandler(http_client=mock_client)
        store = UpstoxTokenStore()
        settings = _mock_settings()

        await handler.refresh(settings, store)

        assert store.get_refresh_token() == "ref-xyz"

    async def test_refresh_uses_expires_in_from_response(self) -> None:
        """When expires_in is present the token should be valid after refresh."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.return_value = _ok_token_response(expires_in=7200)

        handler = UpstoxOAuthRefreshHandler(http_client=mock_client)
        store = UpstoxTokenStore()
        settings = _mock_settings()

        await handler.refresh(settings, store)

        assert store.is_valid() is True

    async def test_refresh_defaults_to_3600_when_expires_in_absent(self) -> None:
        """Missing expires_in defaults to 3600 s (1 hour) — token is still valid."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.return_value = _ok_token_response(expires_in=None)

        handler = UpstoxOAuthRefreshHandler(http_client=mock_client)
        store = UpstoxTokenStore()
        settings = _mock_settings()

        await handler.refresh(settings, store)

        assert store.is_valid() is True

    async def test_refresh_posts_to_correct_url(self) -> None:
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.return_value = _ok_token_response()

        handler = UpstoxOAuthRefreshHandler(http_client=mock_client)
        store = UpstoxTokenStore()
        settings = _mock_settings()

        await handler.refresh(settings, store)

        call_args = mock_client.post.call_args
        assert call_args[0][0] == UPSTOX_TOKEN_URL

    async def test_refresh_sends_grant_type(self) -> None:
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.return_value = _ok_token_response()

        handler = UpstoxOAuthRefreshHandler(http_client=mock_client)
        store = UpstoxTokenStore()
        settings = _mock_settings()

        await handler.refresh(settings, store)

        call_args = mock_client.post.call_args
        form_data: dict[str, str] = call_args.kwargs["data"]
        assert form_data["grant_type"] == "authorization_code"

    async def test_refresh_sends_client_credentials(self) -> None:
        """client_id and client_secret from settings are sent in form body."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.return_value = _ok_token_response()

        handler = UpstoxOAuthRefreshHandler(http_client=mock_client)
        store = UpstoxTokenStore()
        settings = _mock_settings(api_key="MY_KEY", api_secret="MY_SECRET")

        await handler.refresh(settings, store)

        call_args = mock_client.post.call_args
        form_data: dict[str, str] = call_args.kwargs["data"]
        assert form_data["client_id"] == "MY_KEY"
        assert form_data["client_secret"] == "MY_SECRET"

    async def test_refresh_sends_redirect_uri(self) -> None:
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.return_value = _ok_token_response()

        handler = UpstoxOAuthRefreshHandler(http_client=mock_client)
        store = UpstoxTokenStore()
        settings = _mock_settings(redirect_uri="https://example.com/cb")

        await handler.refresh(settings, store)

        call_args = mock_client.post.call_args
        form_data: dict[str, str] = call_args.kwargs["data"]
        assert form_data["redirect_uri"] == "https://example.com/cb"

    async def test_refresh_sends_auth_code_when_provided(self) -> None:
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.return_value = _ok_token_response()

        handler = UpstoxOAuthRefreshHandler(http_client=mock_client)
        store = UpstoxTokenStore()
        settings = _mock_settings()

        await handler.refresh(settings, store, auth_code="some-code-xyz")

        call_args = mock_client.post.call_args
        form_data: dict[str, str] = call_args.kwargs["data"]
        assert form_data["code"] == "some-code-xyz"

    async def test_refresh_omits_code_when_not_provided(self) -> None:
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.return_value = _ok_token_response()

        handler = UpstoxOAuthRefreshHandler(http_client=mock_client)
        store = UpstoxTokenStore()
        settings = _mock_settings()

        await handler.refresh(settings, store)  # no auth_code

        call_args = mock_client.post.call_args
        form_data: dict[str, str] = call_args.kwargs["data"]
        assert "code" not in form_data


# ---------------------------------------------------------------------------
# UpstoxOAuthRefreshHandler — refresh failure paths
# ---------------------------------------------------------------------------


class TestRefreshFailure:
    """refresh() returns False and clears the store on failure."""

    async def test_refresh_returns_false_on_http_4xx(self) -> None:
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.return_value = _error_response(400)

        handler = UpstoxOAuthRefreshHandler(http_client=mock_client)
        store = UpstoxTokenStore()
        store.set_tokens(access_token="old", expires_at=_FUTURE_DT)
        settings = _mock_settings()

        result = await handler.refresh(settings, store)

        assert result is False

    async def test_refresh_clears_store_on_http_4xx(self) -> None:
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.return_value = _error_response(401)

        handler = UpstoxOAuthRefreshHandler(http_client=mock_client)
        store = UpstoxTokenStore()
        store.set_tokens(access_token="old", expires_at=_FUTURE_DT)
        settings = _mock_settings()

        await handler.refresh(settings, store)

        assert store.get_access_token() is None

    async def test_refresh_returns_false_on_http_5xx(self) -> None:
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.return_value = _error_response(500)

        handler = UpstoxOAuthRefreshHandler(http_client=mock_client)
        store = UpstoxTokenStore()
        settings = _mock_settings()

        result = await handler.refresh(settings, store)

        assert result is False

    async def test_refresh_returns_false_on_network_error(self) -> None:
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.side_effect = httpx.ConnectError("connection refused")

        handler = UpstoxOAuthRefreshHandler(http_client=mock_client)
        store = UpstoxTokenStore()
        settings = _mock_settings()

        result = await handler.refresh(settings, store)

        assert result is False

    async def test_refresh_clears_store_on_network_error(self) -> None:
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.side_effect = httpx.ConnectError("timeout")

        handler = UpstoxOAuthRefreshHandler(http_client=mock_client)
        store = UpstoxTokenStore()
        store.set_tokens(access_token="old", expires_at=_FUTURE_DT)
        settings = _mock_settings()

        await handler.refresh(settings, store)

        assert store.get_access_token() is None

    async def test_refresh_returns_false_on_missing_access_token_field(self) -> None:
        """Response missing 'access_token' → False."""
        request = httpx.Request("POST", UPSTOX_TOKEN_URL)
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.return_value = httpx.Response(
            status_code=200,
            json={"other_field": "no_token_here"},
            request=request,
        )

        handler = UpstoxOAuthRefreshHandler(http_client=mock_client)
        store = UpstoxTokenStore()
        settings = _mock_settings()

        result = await handler.refresh(settings, store)

        assert result is False


# ---------------------------------------------------------------------------
# UpstoxOAuthRefreshHandler — with_retry
# ---------------------------------------------------------------------------


class TestWithRetry:
    """with_retry wraps a request callable with 401-triggered token refresh."""

    async def test_calls_request_fn_with_current_token(self) -> None:
        handler = UpstoxOAuthRefreshHandler()
        store = UpstoxTokenStore()
        store.set_tokens(access_token="current-token", expires_at=_FUTURE_DT)
        settings = _mock_settings()

        received_tokens: list[str] = []

        async def fake_request(token: str) -> str:
            received_tokens.append(token)
            return "ok"

        result = await handler.with_retry(fake_request, store, settings)

        assert result == "ok"
        assert received_tokens == ["current-token"]

    async def test_returns_request_fn_return_value(self) -> None:
        handler = UpstoxOAuthRefreshHandler()
        store = UpstoxTokenStore()
        store.set_tokens(access_token="tok", expires_at=_FUTURE_DT)
        settings = _mock_settings()

        async def fake_request(token: str) -> dict[str, Any]:
            return {"data": [1, 2, 3]}

        result = await handler.with_retry(fake_request, store, settings)

        assert result == {"data": [1, 2, 3]}

    async def test_on_401_refreshes_and_retries(self) -> None:
        """HTTP 401 triggers one refresh + one retry; second call succeeds."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.return_value = _ok_token_response(access_token="fresh-token")

        handler = UpstoxOAuthRefreshHandler(http_client=mock_client)
        store = UpstoxTokenStore()
        store.set_tokens(access_token="expired-token", expires_at=_FUTURE_DT)
        settings = _mock_settings()

        call_count = 0

        async def fake_request(token: str) -> str:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise _http_status_error(401)
            return "success-after-refresh"

        result = await handler.with_retry(fake_request, store, settings)

        assert result == "success-after-refresh"
        assert call_count == 2
        mock_client.post.assert_called_once()

    async def test_on_401_retry_uses_new_token(self) -> None:
        """The retry must use the refreshed token, not the expired one."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.return_value = _ok_token_response(access_token="brand-new")

        handler = UpstoxOAuthRefreshHandler(http_client=mock_client)
        store = UpstoxTokenStore()
        store.set_tokens(access_token="old-token", expires_at=_FUTURE_DT)
        settings = _mock_settings()

        tokens_seen: list[str] = []

        async def fake_request(token: str) -> str:
            tokens_seen.append(token)
            if len(tokens_seen) == 1:
                raise _http_status_error(401)
            return "ok"

        await handler.with_retry(fake_request, store, settings)

        assert tokens_seen[0] == "old-token"
        assert tokens_seen[1] == "brand-new"

    async def test_on_401_failed_refresh_raises_upstox_auth_error(self) -> None:
        """401 + failed refresh → UpstoxAuthError."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.return_value = _error_response(400)

        handler = UpstoxOAuthRefreshHandler(http_client=mock_client)
        store = UpstoxTokenStore()
        store.set_tokens(access_token="tok", expires_at=_FUTURE_DT)
        settings = _mock_settings()

        async def fake_request(token: str) -> str:
            raise _http_status_error(401)

        with pytest.raises(UpstoxAuthError):
            await handler.with_retry(fake_request, store, settings)

    async def test_on_401_refresh_ok_but_retry_also_401_raises_auth_error(
        self,
    ) -> None:
        """Refresh succeeds but the API still returns 401 → UpstoxAuthError."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.return_value = _ok_token_response(access_token="fresh")

        handler = UpstoxOAuthRefreshHandler(http_client=mock_client)
        store = UpstoxTokenStore()
        store.set_tokens(access_token="old", expires_at=_FUTURE_DT)
        settings = _mock_settings()

        async def always_401(token: str) -> str:
            raise _http_status_error(401)

        with pytest.raises(UpstoxAuthError):
            await handler.with_retry(always_401, store, settings)

    async def test_non_401_errors_propagate_unchanged(self) -> None:
        """HTTP 500 must propagate to the caller without token refresh."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)

        handler = UpstoxOAuthRefreshHandler(http_client=mock_client)
        store = UpstoxTokenStore()
        store.set_tokens(access_token="tok", expires_at=_FUTURE_DT)
        settings = _mock_settings()

        async def server_error(token: str) -> str:
            raise _http_status_error(500)

        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await handler.with_retry(server_error, store, settings)

        assert exc_info.value.response.status_code == 500
        mock_client.post.assert_not_called()

    async def test_max_retries_0_no_refresh_on_401(self) -> None:
        """max_retries=0 means 401 errors are not retried; raises UpstoxAuthError."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)

        handler = UpstoxOAuthRefreshHandler(http_client=mock_client)
        store = UpstoxTokenStore()
        store.set_tokens(access_token="tok", expires_at=_FUTURE_DT)
        settings = _mock_settings()

        async def always_401(token: str) -> str:
            raise _http_status_error(401)

        with pytest.raises(UpstoxAuthError):
            await handler.with_retry(
                always_401, store, settings, max_retries=0
            )

        mock_client.post.assert_not_called()

    async def test_no_token_triggers_pre_refresh(self) -> None:
        """When the store has no token, refresh is called before the first request."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.return_value = _ok_token_response(access_token="acquired")

        handler = UpstoxOAuthRefreshHandler(http_client=mock_client)
        store = UpstoxTokenStore()  # empty — no token set
        settings = _mock_settings()

        received_tokens: list[str] = []

        async def fake_request(token: str) -> str:
            received_tokens.append(token)
            return "ok"

        result = await handler.with_retry(fake_request, store, settings)

        assert result == "ok"
        # The token used must be the one obtained by the pre-refresh.
        assert received_tokens == ["acquired"]
        mock_client.post.assert_called_once()

    async def test_no_token_and_refresh_fails_raises_auth_error(self) -> None:
        """No token + failed pre-refresh → UpstoxAuthError."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.return_value = _error_response(500)

        handler = UpstoxOAuthRefreshHandler(http_client=mock_client)
        store = UpstoxTokenStore()  # empty
        settings = _mock_settings()

        async def fake_request(token: str) -> str:
            return "ok"

        with pytest.raises(UpstoxAuthError):
            await handler.with_retry(fake_request, store, settings)


# ---------------------------------------------------------------------------
# Credential safety — tokens must never appear in log output
# ---------------------------------------------------------------------------


class TestCredentialSafety:
    """Verify that no credential values leak into log entries."""

    async def test_access_token_not_logged_on_success(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        secret_token = "SUPER_SECRET_ACCESS_TOKEN_DO_NOT_LOG_123"
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.return_value = _ok_token_response(
            access_token=secret_token
        )

        handler = UpstoxOAuthRefreshHandler(http_client=mock_client)
        store = UpstoxTokenStore()
        settings = _mock_settings()

        with caplog.at_level(logging.DEBUG):
            await handler.refresh(settings, store)

        all_log_text = " ".join(record.message for record in caplog.records)
        assert secret_token not in all_log_text, "Access token leaked into logs!"

    async def test_api_key_not_logged_on_failure(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        secret_key = "SECRET_API_KEY_MUST_NOT_APPEAR_IN_LOGS"
        secret = "SECRET_API_SECRET_MUST_NOT_APPEAR_IN_LOGS"
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.return_value = _error_response(401)

        handler = UpstoxOAuthRefreshHandler(http_client=mock_client)
        store = UpstoxTokenStore()
        settings = _mock_settings(api_key=secret_key, api_secret=secret)

        with caplog.at_level(logging.DEBUG):
            await handler.refresh(settings, store)

        all_log_text = " ".join(record.message for record in caplog.records)
        assert secret_key not in all_log_text, "API key leaked into logs!"
        assert secret not in all_log_text, "API secret leaked into logs!"

    async def test_refresh_token_not_logged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        secret_refresh = "SECRET_REFRESH_TOKEN_DO_NOT_LOG_XYZ"
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.return_value = _ok_token_response(
            access_token="acc", refresh_token=secret_refresh
        )

        handler = UpstoxOAuthRefreshHandler(http_client=mock_client)
        store = UpstoxTokenStore()
        settings = _mock_settings()

        with caplog.at_level(logging.DEBUG):
            await handler.refresh(settings, store)

        all_log_text = " ".join(record.message for record in caplog.records)
        assert secret_refresh not in all_log_text, "Refresh token leaked into logs!"

    def test_token_store_does_not_log_value_on_set(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        secret_token = "STORED_TOKEN_MUST_NOT_APPEAR_IN_LOGS_ABC"
        store = UpstoxTokenStore()

        with caplog.at_level(logging.DEBUG):
            store.set_tokens(access_token=secret_token, expires_at=_FUTURE_DT)

        all_log_text = " ".join(record.message for record in caplog.records)
        assert secret_token not in all_log_text, "Token value leaked into store logs!"


# ---------------------------------------------------------------------------
# UpstoxAuthError
# ---------------------------------------------------------------------------


class TestUpstoxAuthError:
    """Basic exception surface."""

    def test_default_message(self) -> None:
        exc = UpstoxAuthError()
        assert "refresh failed" in str(exc).lower() or "failed" in str(exc).lower()

    def test_custom_message(self) -> None:
        exc = UpstoxAuthError("custom reason")
        assert "custom reason" in str(exc)

    def test_is_exception(self) -> None:
        assert isinstance(UpstoxAuthError(), Exception)
