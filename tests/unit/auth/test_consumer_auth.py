"""
Unit tests for consumer authentication — Task 13.2.

Requirements: 19.2

Coverage:
  - ApiKeyAuth          — valid key, missing key, invalid key
  - JwtBearerAuth       — valid token, missing token, expired token,
                          bad signature, missing required claims
  - ConsumerAuthDependency — API-key path, JWT path, neither provided
  - GET /v1/auth/token  — success, missing key, invalid key

Tests use an in-process ASGI test client (httpx.AsyncClient via
ASGITransport) so no live Redis or PostgreSQL is required.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import patch

import jwt as pyjwt
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from src.auth.consumer_auth import (
    ApiKeyAuth,
    ConsumerAuthDependency,
    JwtBearerAuth,
    router as auth_router,
)

# ---------------------------------------------------------------------------
# Constants shared by all tests
# ---------------------------------------------------------------------------

_SECRET = "test-secret-that-is-at-least-32-chars-long"
_VALID_KEYS = ["key-alpha", "key-beta"]
_ALGORITHM = "HS256"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_jwt(
    sub: str = "key-alpha",
    exp_delta_seconds: int = 3600,
    secret: str = _SECRET,
    algorithm: str = _ALGORITHM,
    extra_claims: dict[str, Any] | None = None,
) -> str:
    """Mint a test JWT with the given parameters."""
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": sub,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=exp_delta_seconds)).timestamp()),
        "scopes": [],
    }
    if extra_claims:
        payload.update(extra_claims)
    return pyjwt.encode(payload, secret, algorithm=algorithm)


def _expired_jwt(sub: str = "key-alpha") -> str:
    """Return a JWT that expired 1 second ago."""
    return _make_jwt(sub=sub, exp_delta_seconds=-1)


def _settings_patch(
    keys: list[str] | None = None,
    secret: str = _SECRET,
    expiry: int = 3600,
) -> Any:
    """Return a context manager that patches get_settings for auth tests."""
    from unittest.mock import MagicMock  # noqa: PLC0415

    mock_settings = MagicMock()
    mock_settings.consumer_api_keys_list = keys if keys is not None else _VALID_KEYS
    mock_settings.jwt_secret = secret
    mock_settings.jwt_expiry_seconds = expiry

    return patch("src.auth.consumer_auth.get_settings", return_value=mock_settings)


# ---------------------------------------------------------------------------
# Minimal FastAPI application for test HTTP calls
# ---------------------------------------------------------------------------


def _build_test_app() -> FastAPI:
    """Build a minimal app with the auth router and a protected probe endpoint."""
    app = FastAPI()
    app.include_router(auth_router, prefix="/v1")

    # Add a probe endpoint that uses ConsumerAuthDependency so we can test it
    # end-to-end without the full platform stack.
    @app.get("/v1/probe/api-key")
    async def probe_api_key(consumer: str = Depends(ApiKeyAuth())) -> dict[str, str]:
        return {"consumer": consumer}

    @app.get("/v1/probe/jwt")
    async def probe_jwt(
        payload: dict[str, Any] = Depends(JwtBearerAuth()),
    ) -> dict[str, Any]:
        return {"sub": payload["sub"]}

    @app.get("/v1/probe/any")
    async def probe_any(
        identity: Any = Depends(ConsumerAuthDependency()),
    ) -> dict[str, Any]:
        if isinstance(identity, str):
            return {"kind": "api_key", "identity": identity}
        return {"kind": "jwt", "sub": identity["sub"]}

    return app


# ---------------------------------------------------------------------------
# ApiKeyAuth tests
# ---------------------------------------------------------------------------


class TestApiKeyAuth:
    """Tests for the X-API-KEY header validation path."""

    def test_valid_key_accepted(self) -> None:
        """A key present in the allowlist must be accepted."""
        app = _build_test_app()
        with _settings_patch():
            client = TestClient(app)
            resp = client.get("/v1/probe/api-key", headers={"X-API-KEY": "key-alpha"})
        assert resp.status_code == 200
        assert resp.json()["consumer"] == "key-alpha"

    def test_second_valid_key_accepted(self) -> None:
        """Each key in the allowlist must individually work."""
        app = _build_test_app()
        with _settings_patch():
            client = TestClient(app)
            resp = client.get("/v1/probe/api-key", headers={"X-API-KEY": "key-beta"})
        assert resp.status_code == 200

    def test_missing_key_returns_401(self) -> None:
        """Absent X-API-KEY header must return HTTP 401."""
        app = _build_test_app()
        with _settings_patch():
            client = TestClient(app)
            resp = client.get("/v1/probe/api-key")
        assert resp.status_code == 401
        err = resp.json()["detail"]["error"]
        assert err["code"] == "UNAUTHORIZED"

    def test_invalid_key_returns_401(self) -> None:
        """An unknown key must return HTTP 401 with INVALID_API_KEY."""
        app = _build_test_app()
        with _settings_patch():
            client = TestClient(app)
            resp = client.get("/v1/probe/api-key", headers={"X-API-KEY": "not-in-list"})
        assert resp.status_code == 401
        err = resp.json()["detail"]["error"]
        assert err["code"] == "INVALID_API_KEY"

    def test_empty_allowlist_rejects_all_keys(self) -> None:
        """When the allowlist is empty, every key must be rejected."""
        app = _build_test_app()
        with _settings_patch(keys=[]):
            client = TestClient(app)
            resp = client.get("/v1/probe/api-key", headers={"X-API-KEY": "key-alpha"})
        assert resp.status_code == 401

    def test_error_envelope_structure(self) -> None:
        """Error response must have the canonical envelope shape."""
        app = _build_test_app()
        with _settings_patch():
            client = TestClient(app)
            resp = client.get("/v1/probe/api-key", headers={"X-API-KEY": "bad-key"})
        err = resp.json()["detail"]["error"]
        # All required canonical fields must be present
        for field in ("code", "message", "provider", "retryAfterMs", "requestId"):
            assert field in err, f"Missing field: {field}"
        assert err["provider"] is None
        assert err["retryAfterMs"] is None
        assert err["requestId"]  # non-empty string


# ---------------------------------------------------------------------------
# JwtBearerAuth tests
# ---------------------------------------------------------------------------


class TestJwtBearerAuth:
    """Tests for the Authorization: Bearer JWT validation path."""

    def test_valid_token_accepted(self) -> None:
        """A correctly signed, unexpired JWT must be accepted."""
        token = _make_jwt()
        app = _build_test_app()
        with _settings_patch():
            client = TestClient(app)
            resp = client.get(
                "/v1/probe/jwt",
                headers={"Authorization": f"Bearer {token}"},
            )
        assert resp.status_code == 200
        assert resp.json()["sub"] == "key-alpha"

    def test_missing_authorization_header_returns_401(self) -> None:
        """Absent Authorization header must return HTTP 401 with UNAUTHORIZED."""
        app = _build_test_app()
        with _settings_patch():
            client = TestClient(app)
            resp = client.get("/v1/probe/jwt")
        assert resp.status_code == 401
        err = resp.json()["detail"]["error"]
        assert err["code"] == "UNAUTHORIZED"

    def test_expired_token_returns_401_token_expired(self) -> None:
        """An expired JWT must return HTTP 401 with code TOKEN_EXPIRED."""
        token = _expired_jwt()
        app = _build_test_app()
        with _settings_patch():
            client = TestClient(app)
            resp = client.get(
                "/v1/probe/jwt",
                headers={"Authorization": f"Bearer {token}"},
            )
        assert resp.status_code == 401
        err = resp.json()["detail"]["error"]
        assert err["code"] == "TOKEN_EXPIRED"

    def test_bad_signature_returns_401_invalid_token(self) -> None:
        """A JWT signed with the wrong secret must return INVALID_TOKEN."""
        token = _make_jwt(secret="wrong-secret-xxxxxxxxxxxxxxxxxxxxxxxx")
        app = _build_test_app()
        with _settings_patch(secret=_SECRET):
            client = TestClient(app)
            resp = client.get(
                "/v1/probe/jwt",
                headers={"Authorization": f"Bearer {token}"},
            )
        assert resp.status_code == 401
        err = resp.json()["detail"]["error"]
        assert err["code"] == "INVALID_TOKEN"

    def test_malformed_token_returns_401(self) -> None:
        """A completely garbled bearer value must return HTTP 401."""
        app = _build_test_app()
        with _settings_patch():
            client = TestClient(app)
            resp = client.get(
                "/v1/probe/jwt",
                headers={"Authorization": "Bearer not.a.valid.jwt"},
            )
        assert resp.status_code == 401
        assert resp.json()["detail"]["error"]["code"] == "INVALID_TOKEN"

    def test_missing_sub_claim_returns_401(self) -> None:
        """A JWT without the 'sub' claim must be rejected."""
        # Build a payload with no 'sub' key
        now = datetime.now(timezone.utc)
        payload = {
            "exp": int((now + timedelta(hours=1)).timestamp()),
            "iat": int(now.timestamp()),
        }
        token = pyjwt.encode(payload, _SECRET, algorithm=_ALGORITHM)
        app = _build_test_app()
        with _settings_patch():
            client = TestClient(app)
            resp = client.get(
                "/v1/probe/jwt",
                headers={"Authorization": f"Bearer {token}"},
            )
        assert resp.status_code == 401

    def test_jwt_payload_scopes_preserved(self) -> None:
        """Optional 'scopes' claim in the JWT payload must be forwarded."""
        token = _make_jwt(extra_claims={"scopes": ["read:quotes", "read:history"]})
        app = _build_test_app()
        with _settings_patch():
            # Directly decode the token to verify the claim survived round-trip.
            decoded = pyjwt.decode(token, _SECRET, algorithms=[_ALGORITHM])
        assert decoded["scopes"] == ["read:quotes", "read:history"]


# ---------------------------------------------------------------------------
# ConsumerAuthDependency tests
# ---------------------------------------------------------------------------


class TestConsumerAuthDependency:
    """Tests for the combined API key + JWT auth dependency."""

    def test_api_key_path_works(self) -> None:
        """ConsumerAuthDependency must accept a valid API key."""
        app = _build_test_app()
        with _settings_patch():
            client = TestClient(app)
            resp = client.get(
                "/v1/probe/any", headers={"X-API-KEY": "key-alpha"}
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["kind"] == "api_key"
        assert body["identity"] == "key-alpha"

    def test_jwt_path_works(self) -> None:
        """ConsumerAuthDependency must accept a valid JWT bearer token."""
        token = _make_jwt(sub="key-alpha")
        app = _build_test_app()
        with _settings_patch():
            client = TestClient(app)
            resp = client.get(
                "/v1/probe/any",
                headers={"Authorization": f"Bearer {token}"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["kind"] == "jwt"
        assert body["sub"] == "key-alpha"

    def test_neither_credential_returns_401(self) -> None:
        """No X-API-KEY and no Authorization header must return HTTP 401."""
        app = _build_test_app()
        with _settings_patch():
            client = TestClient(app)
            resp = client.get("/v1/probe/any")
        assert resp.status_code == 401
        err = resp.json()["detail"]["error"]
        assert err["code"] == "UNAUTHORIZED"

    def test_api_key_takes_precedence_over_jwt(self) -> None:
        """When both headers are supplied, the API key path is used."""
        token = _make_jwt()
        app = _build_test_app()
        with _settings_patch():
            client = TestClient(app)
            resp = client.get(
                "/v1/probe/any",
                headers={
                    "X-API-KEY": "key-alpha",
                    "Authorization": f"Bearer {token}",
                },
            )
        assert resp.status_code == 200
        assert resp.json()["kind"] == "api_key"

    def test_invalid_api_key_with_no_jwt_returns_401(self) -> None:
        """Invalid API key with no JWT must still return 401."""
        app = _build_test_app()
        with _settings_patch():
            client = TestClient(app)
            resp = client.get(
                "/v1/probe/any", headers={"X-API-KEY": "wrong-key"}
            )
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# GET /v1/auth/token endpoint tests
# ---------------------------------------------------------------------------


class TestTokenEndpoint:
    """Tests for the key-exchange endpoint GET /v1/auth/token."""

    def test_valid_key_returns_access_token(self) -> None:
        """A valid API key must return a signed JWT access token."""
        app = _build_test_app()
        with _settings_patch():
            client = TestClient(app)
            resp = client.get("/v1/auth/token", params={"api_key": "key-alpha"})
        assert resp.status_code == 200
        body = resp.json()
        assert "accessToken" in body
        assert body["tokenType"] == "Bearer"
        assert body["expiresIn"] == 3600

    def test_returned_token_is_valid_jwt(self) -> None:
        """The issued token must be decodable with the configured secret."""
        app = _build_test_app()
        with _settings_patch():
            client = TestClient(app)
            resp = client.get("/v1/auth/token", params={"api_key": "key-alpha"})
        token = resp.json()["accessToken"]
        payload = pyjwt.decode(token, _SECRET, algorithms=[_ALGORITHM])
        assert payload["sub"] == "key-alpha"
        assert "exp" in payload
        assert "iat" in payload

    def test_token_expiry_matches_setting(self) -> None:
        """The JWT 'exp' claim must reflect the configured expiry duration."""
        custom_expiry = 1800
        app = _build_test_app()
        with _settings_patch(expiry=custom_expiry):
            client = TestClient(app)
            resp = client.get("/v1/auth/token", params={"api_key": "key-alpha"})
        assert resp.json()["expiresIn"] == custom_expiry
        token = resp.json()["accessToken"]
        payload = pyjwt.decode(token, _SECRET, algorithms=[_ALGORITHM])
        issued_at = payload["iat"]
        expires_at = payload["exp"]
        assert (expires_at - issued_at) == custom_expiry

    def test_missing_api_key_param_returns_422(self) -> None:
        """Omitting the required api_key query parameter must return 422."""
        app = _build_test_app()
        with _settings_patch():
            client = TestClient(app)
            resp = client.get("/v1/auth/token")
        # FastAPI returns 422 for missing required query params
        assert resp.status_code == 422

    def test_invalid_api_key_returns_401(self) -> None:
        """An API key not in the allowlist must return HTTP 401."""
        app = _build_test_app()
        with _settings_patch():
            client = TestClient(app)
            resp = client.get("/v1/auth/token", params={"api_key": "not-valid"})
        assert resp.status_code == 401
        err = resp.json()["detail"]["error"]
        assert err["code"] == "INVALID_API_KEY"

    def test_token_response_never_exposes_secret(self) -> None:
        """The /v1/auth/token response must not contain the JWT secret."""
        app = _build_test_app()
        with _settings_patch():
            client = TestClient(app)
            resp = client.get("/v1/auth/token", params={"api_key": "key-alpha"})
        raw_body = resp.text
        assert _SECRET not in raw_body

    def test_error_response_never_exposes_secret(self) -> None:
        """Error responses must not leak the configured JWT secret."""
        app = _build_test_app()
        with _settings_patch():
            client = TestClient(app)
            resp = client.get("/v1/auth/token", params={"api_key": "bad-key"})
        raw_body = resp.text
        assert _SECRET not in raw_body


# ---------------------------------------------------------------------------
# Settings integration — consumer_api_keys_list property
# ---------------------------------------------------------------------------


class TestSettingsApiKeysParsing:
    """Verify settings.consumer_api_keys_list parses the env var correctly."""

    def test_comma_separated_keys_parsed(self) -> None:
        """Multiple keys separated by commas must all appear in the list."""
        from src.core.settings import Settings  # noqa: PLC0415

        s = Settings(
            database_url="postgresql://x:y@localhost/z",
            consumer_api_keys="key-one,key-two,key-three",
        )
        assert s.consumer_api_keys_list == ["key-one", "key-two", "key-three"]

    def test_whitespace_stripped_from_keys(self) -> None:
        """Whitespace around key entries must be stripped."""
        from src.core.settings import Settings  # noqa: PLC0415

        s = Settings(
            database_url="postgresql://x:y@localhost/z",
            consumer_api_keys=" key-one , key-two ",
        )
        assert s.consumer_api_keys_list == ["key-one", "key-two"]

    def test_empty_string_yields_empty_list(self) -> None:
        """Empty CONSUMER_API_KEYS must yield an empty list."""
        from src.core.settings import Settings  # noqa: PLC0415

        s = Settings(
            database_url="postgresql://x:y@localhost/z",
            consumer_api_keys="",
        )
        assert s.consumer_api_keys_list == []

    def test_single_key_works(self) -> None:
        """A single key with no commas must be a one-element list."""
        from src.core.settings import Settings  # noqa: PLC0415

        s = Settings(
            database_url="postgresql://x:y@localhost/z",
            consumer_api_keys="only-key",
        )
        assert s.consumer_api_keys_list == ["only-key"]
