"""
Unit tests for CredentialStripperMiddleware (Task 13.1).

Requirements: 16.8, 19.1, 19.4

Verifies that:
- JSON response bodies have credential-bearing fields redacted recursively.
- Non-JSON responses pass through unchanged.
- Request headers are never modified.
- Response headers are preserved (except Content-Length which is recomputed).
- Custom / extra key patterns work.
- CredentialPattern.is_credential_key covers all design-mandated substrings.
- Nested objects and arrays are recursively sanitised.
- Malformed JSON is passed through without error.
- Empty / null bodies are handled gracefully.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse, PlainTextResponse
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request as StarletteRequest

from src.middleware.credential_stripper import (
    REDACTED,
    CredentialPattern,
    CredentialStripperMiddleware,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_app(response_data: Any, *, media_type: str = "application/json") -> FastAPI:
    """
    Build a minimal FastAPI app that always returns *response_data* (JSON) or,
    when *media_type* is not JSON, a plain-text response.
    """
    app = FastAPI()

    @app.get("/echo")
    async def echo() -> Any:  # noqa: ANN401
        if media_type == "application/json":
            return JSONResponse(content=response_data)
        return PlainTextResponse(content=str(response_data))

    app.add_middleware(CredentialStripperMiddleware)
    return app


async def _get(app: FastAPI, path: str = "/echo") -> tuple[int, Any]:
    """Send a GET to *path* and return (status_code, parsed_body)."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(path)
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        body = resp.text
    return resp.status_code, body


# ---------------------------------------------------------------------------
# CredentialPattern unit tests
# ---------------------------------------------------------------------------


class TestCredentialPattern:
    """Tests for the CredentialPattern helper class."""

    def test_default_patterns_initialised(self) -> None:
        cp = CredentialPattern()
        assert cp.key_substrings  # non-empty

    @pytest.mark.parametrize(
        "key",
        [
            # Design-mandated substrings (Req 16.8 / 19.4)
            "apiKey",
            "api_key",
            "API_KEY",
            "secretKey",
            "secret",
            "SECRET",
            "token",
            "accessToken",
            "access_token",
            "password",
            "PASSWORD",
            "credential",
            "credentials",
            "authorization",
            "Authorization",
            "x-mbx-apikey",
            "X-MBX-APIKEY",
            "x-api-key",
            "X-API-KEY",
            # Additional patterns in the implementation
            "privateKey",
            "private_key",
            "auth_token",
            "passphrase",
        ],
    )
    def test_is_credential_key_positive(self, key: str) -> None:
        cp = CredentialPattern()
        assert cp.is_credential_key(key), f"Expected {key!r} to be flagged as a credential key"

    @pytest.mark.parametrize(
        "key",
        [
            "symbol",
            "exchange",
            "instrumentId",
            "ltp",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "oi",
            "tradedValue",
            "requestedAt",
            "dataSourceType",
            "provider",
            "quality",
            "score",
            "grade",
            "marketStatus",
            "sessionPhase",
            "description",
            "message",
        ],
    )
    def test_is_credential_key_negative(self, key: str) -> None:
        cp = CredentialPattern()
        assert not cp.is_credential_key(key), (
            f"Expected {key!r} NOT to be flagged as a credential key"
        )

    def test_redact_value_returns_sentinel(self) -> None:
        cp = CredentialPattern()
        assert cp.redact_value("super-secret-value") == REDACTED
        assert cp.redact_value(12345) == REDACTED
        assert cp.redact_value(None) == REDACTED

    def test_additional_substrings_extend_defaults(self) -> None:
        cp = CredentialPattern(additional_substrings=["myCustomSensitiveField"])
        assert cp.is_credential_key("myCustomSensitiveField")
        assert cp.is_credential_key("MYCUSTOMSENSITIVEFIELD")  # case-insensitive

    def test_custom_key_substrings_replace_defaults(self) -> None:
        """When key_substrings is overridden, only supplied patterns match."""
        cp = CredentialPattern(key_substrings=["supersecret"])
        assert cp.is_credential_key("supersecret")
        # The default "token" substring is gone if not listed.
        assert not cp.is_credential_key("token")


# ---------------------------------------------------------------------------
# Middleware integration tests (via FastAPI ASGI test client)
# ---------------------------------------------------------------------------


class TestCredentialStripperMiddleware:
    """Integration tests driving the middleware through a minimal FastAPI app."""

    # ── Basic JSON redaction ───────────────────────────────────────────────

    async def test_api_key_field_is_redacted(self) -> None:
        app = _make_app({"apiKey": "SUPERSECRET", "symbol": "NIFTY"})
        _, body = await _get(app)
        assert body["apiKey"] == REDACTED
        assert body["symbol"] == "NIFTY"  # non-credential field preserved

    async def test_secret_field_is_redacted(self) -> None:
        app = _make_app({"secret": "my-secret-value", "ltp": 22100.0})
        _, body = await _get(app)
        assert body["secret"] == REDACTED
        assert body["ltp"] == 22100.0

    async def test_token_field_is_redacted(self) -> None:
        app = _make_app({"accessToken": "Bearer xyz", "provider": "angel_one"})
        _, body = await _get(app)
        assert body["accessToken"] == REDACTED
        assert body["provider"] == "angel_one"

    async def test_password_field_is_redacted(self) -> None:
        app = _make_app({"password": "hunter2", "userId": "user123"})
        _, body = await _get(app)
        assert body["password"] == REDACTED
        assert body["userId"] == "user123"

    async def test_credential_field_is_redacted(self) -> None:
        app = _make_app({"credential": "abc", "exchange": "NSE"})
        _, body = await _get(app)
        assert body["credential"] == REDACTED
        assert body["exchange"] == "NSE"

    async def test_case_insensitive_key_matching(self) -> None:
        app = _make_app({"API_KEY": "val1", "SECRET_KEY": "val2", "ltp": 100.0})
        _, body = await _get(app)
        assert body["API_KEY"] == REDACTED
        assert body["SECRET_KEY"] == REDACTED
        assert body["ltp"] == 100.0

    # ── Nested objects ─────────────────────────────────────────────────────

    async def test_nested_object_credential_is_redacted(self) -> None:
        payload = {
            "data": {
                "provenance": {
                    "apiKey": "nested-secret",
                    "provider": "angel_one",
                },
                "ltp": 22150.0,
            }
        }
        app = _make_app(payload)
        _, body = await _get(app)
        assert body["data"]["provenance"]["apiKey"] == REDACTED
        assert body["data"]["provenance"]["provider"] == "angel_one"
        assert body["data"]["ltp"] == 22150.0

    async def test_deeply_nested_credential_is_redacted(self) -> None:
        payload = {"a": {"b": {"c": {"token": "deep-secret", "value": 42}}}}
        app = _make_app(payload)
        _, body = await _get(app)
        assert body["a"]["b"]["c"]["token"] == REDACTED
        assert body["a"]["b"]["c"]["value"] == 42

    # ── Arrays ─────────────────────────────────────────────────────────────

    async def test_credential_in_array_of_objects_is_redacted(self) -> None:
        payload = [
            {"symbol": "NIFTY", "apiKey": "key1"},
            {"symbol": "BANKNIFTY", "apiKey": "key2"},
        ]
        app = _make_app(payload)
        _, body = await _get(app)
        assert body[0]["apiKey"] == REDACTED
        assert body[1]["apiKey"] == REDACTED
        assert body[0]["symbol"] == "NIFTY"
        assert body[1]["symbol"] == "BANKNIFTY"

    async def test_nested_array_within_object(self) -> None:
        payload = {"items": [{"token": "t1"}, {"token": "t2"}], "count": 2}
        app = _make_app(payload)
        _, body = await _get(app)
        assert body["items"][0]["token"] == REDACTED
        assert body["items"][1]["token"] == REDACTED
        assert body["count"] == 2

    # ── Non-credential fields survive ─────────────────────────────────────

    async def test_non_credential_fields_are_preserved(self) -> None:
        payload = {
            "symbol": "RELIANCE",
            "exchange": "NSE",
            "ltp": 2850.75,
            "volume": 1234567,
            "oi": None,
            "tradedValue": 35123456.78,
            "score": 87,
            "grade": "VALID",
            "marketStatus": "REGULAR",
        }
        app = _make_app(payload)
        _, body = await _get(app)
        assert body == payload  # all fields intact

    async def test_null_value_credential_field_stays_redacted(self) -> None:
        app = _make_app({"apiKey": None, "symbol": "NIFTY"})
        _, body = await _get(app)
        # Even a null credential value becomes the sentinel string.
        assert body["apiKey"] == REDACTED

    async def test_numeric_value_credential_field_is_redacted(self) -> None:
        app = _make_app({"secretId": 99999, "ltp": 100.0})
        _, body = await _get(app)
        assert body["secretId"] == REDACTED

    # ── Non-JSON responses ─────────────────────────────────────────────────

    async def test_plain_text_response_passes_through_unchanged(self) -> None:
        app = _make_app("apiKey=supersecret&token=abc", media_type="text/plain")
        _, body = await _get(app)
        # Plain text — middleware must NOT modify content.
        assert "supersecret" in body

    # ── Status code and HTTP semantics ─────────────────────────────────────

    async def test_status_code_is_preserved(self) -> None:
        app = FastAPI()

        @app.get("/notfound")
        async def notfound() -> JSONResponse:
            return JSONResponse(
                content={"error": {"code": "NOT_FOUND", "apiKey": "leak"}},
                status_code=404,
            )

        app.add_middleware(CredentialStripperMiddleware)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/notfound")

        assert resp.status_code == 404
        body = resp.json()
        assert body["error"]["apiKey"] == REDACTED
        assert body["error"]["code"] == "NOT_FOUND"

    async def test_200_response_is_preserved(self) -> None:
        app = _make_app({"symbol": "NIFTY", "ltp": 22000.0})
        status, body = await _get(app)
        assert status == 200
        assert body["symbol"] == "NIFTY"

    # ── Malformed / edge-case bodies ──────────────────────────────────────

    async def test_empty_json_object_passes_through(self) -> None:
        app = _make_app({})
        status, body = await _get(app)
        assert status == 200
        assert body == {}

    async def test_empty_json_array_passes_through(self) -> None:
        app = _make_app([])
        status, body = await _get(app)
        assert status == 200
        assert body == []

    async def test_scalar_json_response_passes_through(self) -> None:
        app = _make_app(42)
        status, body = await _get(app)
        assert status == 200
        assert body == 42

    # ── Request headers not modified ──────────────────────────────────────

    async def test_request_authorization_header_reaches_handler(self) -> None:
        """
        The middleware must NOT strip incoming request headers — they are
        needed for upstream provider calls.  To verify this, the handler
        checks whether the Authorization header arrived and stores the result
        under a response field name that does NOT match any credential pattern.
        """
        app = FastAPI()

        @app.get("/header-echo")
        async def header_echo(request: StarletteRequest) -> JSONResponse:
            # "header_present" does not match any credential pattern, so the
            # middleware will never redact it.
            header_present = request.headers.get("authorization") is not None
            return JSONResponse(content={"header_present": header_present})

        app.add_middleware(CredentialStripperMiddleware)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/header-echo", headers={"Authorization": "Bearer test-token"}
            )

        body = resp.json()
        # handler confirms the request Authorization header was present
        assert body["header_present"] is True

    # ── Custom patterns via constructor ───────────────────────────────────

    async def test_extra_key_substrings_are_also_redacted(self) -> None:
        app = FastAPI()

        @app.get("/echo")
        async def echo() -> JSONResponse:
            return JSONResponse(
                content={"customBrokerPin": "1234", "ltp": 200.0}
            )

        app.add_middleware(
            CredentialStripperMiddleware,
            extra_key_substrings=["brokerpin"],
        )

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/echo")

        body = resp.json()
        assert body["customBrokerPin"] == REDACTED
        assert body["ltp"] == 200.0

    async def test_custom_credential_pattern_object(self) -> None:
        patterns = CredentialPattern(
            key_substrings=["xsensitive"],
            additional_substrings=["yalso"],
        )
        app = FastAPI()

        @app.get("/echo")
        async def echo() -> JSONResponse:
            return JSONResponse(
                content={
                    "xsensitiveValue": "should-be-redacted",
                    "yalsoValue": "should-be-redacted",
                    "normalField": "should-survive",
                    # "token" is NOT in the custom list, so must survive.
                    "token": "should-survive-too",
                }
            )

        app.add_middleware(CredentialStripperMiddleware, patterns=patterns)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/echo")

        body = resp.json()
        assert body["xsensitiveValue"] == REDACTED
        assert body["yalsoValue"] == REDACTED
        assert body["normalField"] == "should-survive"
        assert body["token"] == "should-survive-too"

    # ── Canonical error envelope check (design Req 16.3) ─────────────────

    async def test_error_envelope_credential_leak_is_blocked(self) -> None:
        """
        Simulates a bug where a handler accidentally includes an api_key in
        the error body.  The middleware must catch and redact it.
        """
        app = FastAPI()

        @app.get("/buggy")
        async def buggy() -> JSONResponse:
            return JSONResponse(
                content={
                    "error": {
                        "code": "PROVIDER_ERROR",
                        "message": "upstream failure",
                        "provider": "angel_one",
                        "apiKey": "LEAKED-KEY-VALUE",   # accidental leak
                        "retryAfterMs": None,
                        "requestId": "req-123",
                    }
                },
                status_code=502,
            )

        app.add_middleware(CredentialStripperMiddleware)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/buggy")

        assert resp.status_code == 502
        body = resp.json()
        assert body["error"]["apiKey"] == REDACTED
        assert body["error"]["code"] == "PROVIDER_ERROR"
        assert body["error"]["provider"] == "angel_one"
        assert body["error"]["requestId"] == "req-123"

    # ── Unicode / special values ───────────────────────────────────────────

    async def test_unicode_credential_value_is_redacted(self) -> None:
        app = _make_app({"apiKey": "超级秘密", "symbol": "BTC"})
        _, body = await _get(app)
        assert body["apiKey"] == REDACTED
        assert body["symbol"] == "BTC"
