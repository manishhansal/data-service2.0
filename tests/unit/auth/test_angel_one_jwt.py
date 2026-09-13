"""
tests/unit/auth/test_angel_one_jwt.py

Unit tests for src/auth/angel_one_jwt.py

Tests cover:
- AngelOneJwtStore: get_jwt, set_jwt, is_valid, clear, near-expiry behaviour
- AngelOneJwtRotator.rotate(): success path, HTTP failure paths, malformed
  responses, missing credentials
- AngelOneJwtRotator.start_rotation_task(): task lifecycle, cancellation,
  retry on failure
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.auth.angel_one_jwt import (
    _MAX_ROTATION_RETRIES,
    _NEAR_EXPIRY_WINDOW_SEC,
    AngelOneJwtRotator,
    AngelOneJwtStore,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

UTC = timezone.utc


def _future(hours: float) -> datetime:
    """Return a UTC datetime that many hours in the future."""
    return datetime.now(UTC) + timedelta(hours=hours)


def _past(hours: float) -> datetime:
    """Return a UTC datetime that many hours in the past."""
    return datetime.now(UTC) - timedelta(hours=hours)


def _make_settings(
    api_key: str = "test-api-key",
    client_id: str = "C123456",
    totp_secret: str = "JBSWY3DPEHPK3PXP",  # standard test base32 key
) -> MagicMock:
    """Return a mock Settings object with Angel One credentials."""
    s = MagicMock()
    s.angel_one_api_key = api_key
    s.angel_one_client_id = client_id
    s.angel_one_totp_secret = totp_secret
    return s


def _success_response(jwt_token: str = "eyJtest.jwt.token") -> httpx.Response:
    """Build a mock successful Angel One login HTTP response."""
    body = {
        "status": True,
        "message": "SUCCESS",
        "data": {
            "jwtToken": jwt_token,
            "refreshToken": "refresh-token-value",
        },
    }
    return httpx.Response(200, json=body)


def _error_response(status_code: int, message: str = "error") -> httpx.Response:
    body = {"status": False, "message": message}
    return httpx.Response(status_code, json=body)


# ---------------------------------------------------------------------------
# AngelOneJwtStore
# ---------------------------------------------------------------------------


class TestAngelOneJwtStore:
    """Tests for the in-memory JWT credential store."""

    # --- initial state ---

    def test_initial_get_jwt_returns_none(self) -> None:
        store = AngelOneJwtStore()
        assert store.get_jwt() is None

    def test_initial_is_valid_false(self) -> None:
        store = AngelOneJwtStore()
        assert store.is_valid() is False

    def test_initial_expires_at_none(self) -> None:
        store = AngelOneJwtStore()
        assert store.expires_at is None

    # --- set_jwt + get_jwt ---

    @pytest.mark.asyncio
    async def test_set_and_get_jwt_returns_token(self) -> None:
        store = AngelOneJwtStore()
        token = "eyJ.valid.jwt"
        await store.set_jwt(token, expires_at=_future(24))
        assert store.get_jwt() == token

    @pytest.mark.asyncio
    async def test_is_valid_after_set(self) -> None:
        store = AngelOneJwtStore()
        await store.set_jwt("eyJ.valid.jwt", expires_at=_future(24))
        assert store.is_valid() is True

    @pytest.mark.asyncio
    async def test_expires_at_stored_correctly(self) -> None:
        store = AngelOneJwtStore()
        expiry = _future(24)
        await store.set_jwt("eyJ.token", expires_at=expiry)
        assert store.expires_at == expiry

    # --- expiry semantics ---

    @pytest.mark.asyncio
    async def test_expired_token_returns_none(self) -> None:
        store = AngelOneJwtStore()
        await store.set_jwt("eyJ.old.jwt", expires_at=_past(1))
        assert store.get_jwt() is None

    @pytest.mark.asyncio
    async def test_expired_token_is_valid_false(self) -> None:
        store = AngelOneJwtStore()
        await store.set_jwt("eyJ.old.jwt", expires_at=_past(1))
        assert store.is_valid() is False

    @pytest.mark.asyncio
    async def test_near_expiry_token_returns_none(self) -> None:
        """Token within the 5-minute safety window should be treated as invalid."""
        store = AngelOneJwtStore()
        # Set expiry to 2 minutes from now — within the 5-minute window
        await store.set_jwt("eyJ.soon.jwt", expires_at=_future(2 / 60))
        assert store.get_jwt() is None

    @pytest.mark.asyncio
    async def test_custom_near_expiry_window(self) -> None:
        """Custom near-expiry window is respected."""
        # Window = 0 s — token should be valid right up to the expiry second
        store = AngelOneJwtStore(near_expiry_window_sec=0)
        await store.set_jwt("eyJ.token", expires_at=_future(0.001))  # 3.6s from now
        assert store.get_jwt() is not None

    @pytest.mark.asyncio
    async def test_token_not_within_window_is_valid(self) -> None:
        """Token expiring far in the future should be valid."""
        store = AngelOneJwtStore()
        await store.set_jwt("eyJ.long.jwt", expires_at=_future(23))
        assert store.is_valid() is True

    # --- set_jwt validation ---

    @pytest.mark.asyncio
    async def test_set_jwt_empty_string_raises(self) -> None:
        store = AngelOneJwtStore()
        with pytest.raises(ValueError, match="non-empty"):
            await store.set_jwt("", expires_at=_future(24))

    @pytest.mark.asyncio
    async def test_set_jwt_naive_datetime_raises(self) -> None:
        store = AngelOneJwtStore()
        naive = datetime.now()  # no tzinfo
        with pytest.raises(ValueError, match="timezone-aware"):
            await store.set_jwt("eyJ.token", expires_at=naive)

    # --- clear ---

    @pytest.mark.asyncio
    async def test_clear_removes_jwt(self) -> None:
        store = AngelOneJwtStore()
        await store.set_jwt("eyJ.token", expires_at=_future(24))
        assert store.is_valid() is True
        await store.clear()
        assert store.get_jwt() is None
        assert store.is_valid() is False

    @pytest.mark.asyncio
    async def test_clear_idempotent_on_empty_store(self) -> None:
        store = AngelOneJwtStore()
        await store.clear()  # should not raise
        assert store.get_jwt() is None

    # --- overwrite ---

    @pytest.mark.asyncio
    async def test_set_jwt_overwrites_previous_token(self) -> None:
        store = AngelOneJwtStore()
        await store.set_jwt("eyJ.first", expires_at=_future(24))
        await store.set_jwt("eyJ.second", expires_at=_future(24))
        assert store.get_jwt() == "eyJ.second"


# ---------------------------------------------------------------------------
# AngelOneJwtRotator.rotate()
# ---------------------------------------------------------------------------


class TestAngelOneJwtRotatorRotate:
    """Tests for the single-rotation method."""

    @pytest.mark.asyncio
    async def test_rotate_success_returns_jwt(self) -> None:
        rotator = AngelOneJwtRotator()
        settings = _make_settings()
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.return_value = _success_response("eyJ.fresh.token")
        # Prevent aclose from erroring
        mock_client.aclose = AsyncMock()

        result = await rotator.rotate(settings, http_client=mock_client)

        assert result == "eyJ.fresh.token"

    @pytest.mark.asyncio
    async def test_rotate_sends_correct_payload(self) -> None:
        """TOTP code and clientcode are sent; api_key goes in header."""
        rotator = AngelOneJwtRotator()
        settings = _make_settings(client_id="CLIENTABC")
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.return_value = _success_response()

        with patch("pyotp.TOTP") as mock_totp_cls:
            mock_totp_cls.return_value.now.return_value = "123456"
            await rotator.rotate(settings, http_client=mock_client)

        call_kwargs = mock_client.post.call_args
        sent_json = call_kwargs.kwargs.get("json") or call_kwargs.args[1]
        assert sent_json["clientcode"] == "CLIENTABC"
        assert sent_json["totp"] == "123456"
        assert sent_json["password"] == "123456"

    @pytest.mark.asyncio
    async def test_rotate_api_key_in_header_not_body(self) -> None:
        """API key must go in X-PrivateKey header, never in the request body."""
        rotator = AngelOneJwtRotator()
        settings = _make_settings(api_key="super-secret-key")
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.return_value = _success_response()

        await rotator.rotate(settings, http_client=mock_client)

        call_kwargs = mock_client.post.call_args
        headers = call_kwargs.kwargs.get("headers") or {}
        json_body = call_kwargs.kwargs.get("json") or {}

        assert headers.get("X-PrivateKey") == "super-secret-key"
        # Should not appear in the body
        assert "super-secret-key" not in json.dumps(json_body)

    @pytest.mark.asyncio
    async def test_rotate_missing_credentials_returns_none(self) -> None:
        rotator = AngelOneJwtRotator()
        settings = _make_settings(api_key="", client_id="", totp_secret="")
        mock_client = AsyncMock(spec=httpx.AsyncClient)

        result = await rotator.rotate(settings, http_client=mock_client)

        assert result is None
        mock_client.post.assert_not_called()

    @pytest.mark.asyncio
    async def test_rotate_partial_missing_credentials_returns_none(self) -> None:
        rotator = AngelOneJwtRotator()
        settings = _make_settings(api_key="key", client_id="", totp_secret="secret")
        mock_client = AsyncMock(spec=httpx.AsyncClient)

        result = await rotator.rotate(settings, http_client=mock_client)

        assert result is None

    @pytest.mark.asyncio
    async def test_rotate_http_timeout_returns_none(self) -> None:
        rotator = AngelOneJwtRotator()
        settings = _make_settings()
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.side_effect = httpx.TimeoutException("timed out")

        result = await rotator.rotate(settings, http_client=mock_client)

        assert result is None

    @pytest.mark.asyncio
    async def test_rotate_network_error_returns_none(self) -> None:
        rotator = AngelOneJwtRotator()
        settings = _make_settings()
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.side_effect = httpx.ConnectError("connection refused")

        result = await rotator.rotate(settings, http_client=mock_client)

        assert result is None

    @pytest.mark.asyncio
    async def test_rotate_http_500_returns_none(self) -> None:
        rotator = AngelOneJwtRotator()
        settings = _make_settings()
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.return_value = httpx.Response(500, json={"error": "server error"})

        result = await rotator.rotate(settings, http_client=mock_client)

        assert result is None

    @pytest.mark.asyncio
    async def test_rotate_http_429_returns_none(self) -> None:
        rotator = AngelOneJwtRotator()
        settings = _make_settings()
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.return_value = httpx.Response(429, json={"error": "rate limited"})

        result = await rotator.rotate(settings, http_client=mock_client)

        assert result is None

    @pytest.mark.asyncio
    async def test_rotate_api_status_false_returns_none(self) -> None:
        rotator = AngelOneJwtRotator()
        settings = _make_settings()
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.return_value = _error_response(200, "TOTP mismatch")

        result = await rotator.rotate(settings, http_client=mock_client)

        assert result is None

    @pytest.mark.asyncio
    async def test_rotate_missing_jwt_token_in_response_returns_none(self) -> None:
        rotator = AngelOneJwtRotator()
        settings = _make_settings()
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        # status=True but no jwtToken in data
        mock_client.post.return_value = httpx.Response(
            200, json={"status": True, "data": {"refreshToken": "r"}}
        )

        result = await rotator.rotate(settings, http_client=mock_client)

        assert result is None

    @pytest.mark.asyncio
    async def test_rotate_malformed_json_returns_none(self) -> None:
        rotator = AngelOneJwtRotator()
        settings = _make_settings()
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.return_value = httpx.Response(200, content=b"not-json")

        result = await rotator.rotate(settings, http_client=mock_client)

        assert result is None

    @pytest.mark.asyncio
    async def test_rotate_unexpected_status_returns_none(self) -> None:
        rotator = AngelOneJwtRotator()
        settings = _make_settings()
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.return_value = httpx.Response(302, json={})

        result = await rotator.rotate(settings, http_client=mock_client)

        assert result is None


# ---------------------------------------------------------------------------
# AngelOneJwtRotator._rotate_with_retries()
# ---------------------------------------------------------------------------


class TestAngelOneJwtRotatorWithRetries:
    """Tests for the retry wrapper."""

    @pytest.mark.asyncio
    async def test_succeeds_on_first_attempt(self) -> None:
        rotator = AngelOneJwtRotator()
        rotator.rotate = AsyncMock(return_value="eyJ.token")
        settings = _make_settings()

        result = await rotator._rotate_with_retries(
            settings, max_retries=3, retry_delay_sec=0.0
        )

        assert result == "eyJ.token"
        rotator.rotate.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_retries_on_failure_then_succeeds(self) -> None:
        rotator = AngelOneJwtRotator()
        rotator.rotate = AsyncMock(side_effect=[None, None, "eyJ.token"])
        settings = _make_settings()

        result = await rotator._rotate_with_retries(
            settings, max_retries=3, retry_delay_sec=0.0
        )

        assert result == "eyJ.token"
        assert rotator.rotate.await_count == 3

    @pytest.mark.asyncio
    async def test_returns_none_after_all_retries_fail(self) -> None:
        rotator = AngelOneJwtRotator()
        rotator.rotate = AsyncMock(return_value=None)
        settings = _make_settings()

        result = await rotator._rotate_with_retries(
            settings, max_retries=3, retry_delay_sec=0.0
        )

        assert result is None
        assert rotator.rotate.await_count == 3

    @pytest.mark.asyncio
    async def test_uses_max_rotation_retries_default(self) -> None:
        """Default retry count matches the module constant."""
        rotator = AngelOneJwtRotator()
        rotator.rotate = AsyncMock(return_value=None)
        settings = _make_settings()

        await rotator._rotate_with_retries(settings, retry_delay_sec=0.0)

        assert rotator.rotate.await_count == _MAX_ROTATION_RETRIES


# ---------------------------------------------------------------------------
# AngelOneJwtRotator.start_rotation_task()
# ---------------------------------------------------------------------------


class TestAngelOneJwtRotatorTask:
    """Tests for the background rotation task."""

    @pytest.mark.asyncio
    async def test_start_rotation_task_returns_task(self) -> None:
        rotator = AngelOneJwtRotator()
        store = AngelOneJwtStore()
        settings = _make_settings()

        # Patch _rotation_loop to avoid actual looping
        async def _fake_loop(*args, **kwargs):  # type: ignore[no-untyped-def]
            return

        rotator._rotation_loop = _fake_loop  # type: ignore[method-assign]

        task = await rotator.start_rotation_task(settings, store, interval_hours=23)
        assert isinstance(task, asyncio.Task)
        # Allow the task to complete
        await task

    @pytest.mark.asyncio
    async def test_start_rotation_task_invalid_interval_raises(self) -> None:
        rotator = AngelOneJwtRotator()
        store = AngelOneJwtStore()
        settings = _make_settings()

        with pytest.raises(ValueError, match="interval_hours"):
            await rotator.start_rotation_task(settings, store, interval_hours=0)

    @pytest.mark.asyncio
    async def test_rotation_loop_writes_jwt_to_store(self) -> None:
        """Successful rotation writes the JWT and expiry into the store."""
        rotator = AngelOneJwtRotator()
        store = AngelOneJwtStore()
        settings = _make_settings()

        # Patch rotate to return a token immediately, then cancel after first sleep
        call_count = 0

        async def _fake_rotate(*args, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal call_count
            call_count += 1
            return "eyJ.rotated.token"

        rotator.rotate = _fake_rotate  # type: ignore[method-assign]

        # Run the loop for exactly one cycle (cancel during the sleep)
        task = asyncio.create_task(
            rotator._rotation_loop(settings, store, interval_hours=1)
        )
        # Give the loop time to rotate and enter sleep
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert store.is_valid() is True
        assert store.get_jwt() == "eyJ.rotated.token"

    @pytest.mark.asyncio
    async def test_rotation_loop_cancellation_is_graceful(self) -> None:
        """Cancellation does not raise CancelledError outside the task."""
        rotator = AngelOneJwtRotator()
        store = AngelOneJwtStore()
        settings = _make_settings()

        # Immediately block on sleep — this is normal loop behaviour
        async def _fake_rotate(*args, **kwargs):  # type: ignore[no-untyped-def]
            return "eyJ.token"

        rotator.rotate = _fake_rotate  # type: ignore[method-assign]

        task = asyncio.create_task(
            rotator._rotation_loop(settings, store, interval_hours=1)
        )
        await asyncio.sleep(0.05)
        task.cancel()

        # Should NOT raise — loop swallows CancelledError from the sleep
        result = await asyncio.gather(task, return_exceptions=True)
        assert result == [None]

    @pytest.mark.asyncio
    async def test_rotation_loop_failed_rotation_does_not_crash_loop(self) -> None:
        """A failed rotation cycle (None returned) should not crash the task."""
        rotator = AngelOneJwtRotator()
        store = AngelOneJwtStore()
        settings = _make_settings()

        # Patch _rotate_with_retries (what _rotation_loop actually calls)
        # so we avoid the real 60-second retry sleep inside rotate_with_retries.
        async def _fake_rotate_with_retries(*args, **kwargs):  # type: ignore[no-untyped-def]
            return None  # simulate all retries exhausted

        rotator._rotate_with_retries = _fake_rotate_with_retries  # type: ignore[method-assign]

        task = asyncio.create_task(
            rotator._rotation_loop(settings, store, interval_hours=1)
        )
        await asyncio.sleep(0.05)
        task.cancel()
        result = await asyncio.gather(task, return_exceptions=True)
        # Task cancelled cleanly — None or CancelledError both acceptable
        assert result[0] in (None,) or isinstance(result[0], asyncio.CancelledError)

    @pytest.mark.asyncio
    async def test_rotation_loop_failed_rotation_does_not_overwrite_valid_jwt(
        self,
    ) -> None:
        """When rotation fails, the existing valid JWT in the store is preserved."""
        store = AngelOneJwtStore()
        await store.set_jwt("eyJ.original", expires_at=_future(23))

        rotator = AngelOneJwtRotator()
        settings = _make_settings()

        # Patch _rotate_with_retries to immediately return None (no real delays)
        async def _fake_rotate_with_retries(*args, **kwargs):  # type: ignore[no-untyped-def]
            return None

        rotator._rotate_with_retries = _fake_rotate_with_retries  # type: ignore[method-assign]

        task = asyncio.create_task(
            rotator._rotation_loop(settings, store, interval_hours=1)
        )
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        # Original JWT untouched because the failed rotation didn't call set_jwt
        assert store.get_jwt() == "eyJ.original"


# ---------------------------------------------------------------------------
# Integration: rotate → store round-trip
# ---------------------------------------------------------------------------


class TestRotateAndStoreIntegration:
    """End-to-end: rotator obtains JWT and stores it correctly."""

    @pytest.mark.asyncio
    async def test_rotate_and_store_jwt(self) -> None:
        rotator = AngelOneJwtRotator()
        store = AngelOneJwtStore()
        settings = _make_settings()

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.return_value = _success_response("eyJ.brand.new")

        jwt_token = await rotator.rotate(settings, http_client=mock_client)
        assert jwt_token == "eyJ.brand.new"

        expires_at = _future(24)
        await store.set_jwt(jwt_token, expires_at=expires_at)

        assert store.is_valid() is True
        assert store.get_jwt() == "eyJ.brand.new"

    @pytest.mark.asyncio
    async def test_rotate_failure_leaves_store_empty(self) -> None:
        rotator = AngelOneJwtRotator()
        store = AngelOneJwtStore()
        settings = _make_settings()

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.return_value = httpx.Response(500, json={})

        jwt_token = await rotator.rotate(settings, http_client=mock_client)
        assert jwt_token is None

        # Caller should NOT call store.set_jwt with None
        assert store.get_jwt() is None
        assert store.is_valid() is False
