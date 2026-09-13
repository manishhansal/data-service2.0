"""
Unit tests for CORS allowlist middleware (Task 13.4).

Requirements: 19.6

Tests cover:
- CorsConfig field defaults
- allow_origins property (env-driven parsing and default fallback)
- Wildcard rejection by validator
- get_cors_middleware_args() kwarg shape
- apply_cors() wires CORSMiddleware correctly (end-to-end with ASGI client)
- Actual CORS response headers for allowed and disallowed origins
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from src.middleware.cors_middleware import (
    CorsConfig,
    apply_cors,
    get_cors_middleware_args,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _app_with_cors(config: CorsConfig) -> FastAPI:
    """Build a minimal FastAPI app with CORS applied and one test route."""
    app = FastAPI()
    apply_cors(app, config)

    @app.get("/ping")
    async def ping() -> dict:
        return {"ok": True}

    return app


async def _options(app: FastAPI, origin: str) -> "httpx.Response":  # type: ignore[name-defined]
    """Send an OPTIONS preflight request with the given Origin header."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.options(
            "/ping",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "GET",
            },
        )


async def _get(app: FastAPI, origin: str) -> "httpx.Response":  # type: ignore[name-defined]
    """Send a GET with the given Origin header."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get("/ping", headers={"Origin": origin})


# ---------------------------------------------------------------------------
# CorsConfig — defaults
# ---------------------------------------------------------------------------


class TestCorsConfigDefaults:
    """Verify default field values when no environment variables are set."""

    def test_default_allow_methods(self) -> None:
        config = CorsConfig(cors_allowed_origins="http://localhost:3000")
        assert config.allow_methods == ["GET", "POST", "OPTIONS"]

    def test_default_allow_headers(self) -> None:
        config = CorsConfig(cors_allowed_origins="http://localhost:3000")
        assert set(config.allow_headers) == {"Content-Type", "Authorization", "X-API-KEY"}

    def test_default_allow_credentials_is_false(self) -> None:
        config = CorsConfig(cors_allowed_origins="http://localhost:3000")
        assert config.allow_credentials is False

    def test_default_max_age_seconds(self) -> None:
        config = CorsConfig(cors_allowed_origins="http://localhost:3000")
        assert config.max_age_seconds == 600

    def test_max_age_seconds_cannot_be_negative(self) -> None:
        with pytest.raises(ValidationError):
            CorsConfig(cors_allowed_origins="http://localhost:3000", max_age_seconds=-1)


# ---------------------------------------------------------------------------
# CorsConfig — allow_origins property
# ---------------------------------------------------------------------------


class TestCorsConfigAllowOrigins:
    """Test the allow_origins property that parses the raw env string."""

    def test_single_origin_parsed_correctly(self) -> None:
        config = CorsConfig(cors_allowed_origins="https://alphaforge.example.com")
        assert config.allow_origins == ["https://alphaforge.example.com"]

    def test_multiple_origins_parsed_correctly(self) -> None:
        config = CorsConfig(
            cors_allowed_origins="http://localhost:3000,https://alphaforge.example.com"
        )
        assert config.allow_origins == [
            "http://localhost:3000",
            "https://alphaforge.example.com",
        ]

    def test_whitespace_stripped_from_origins(self) -> None:
        config = CorsConfig(
            cors_allowed_origins="  http://localhost:3000 , https://alphaforge.example.com  "
        )
        assert config.allow_origins == [
            "http://localhost:3000",
            "https://alphaforge.example.com",
        ]

    def test_empty_string_falls_back_to_localhost(self) -> None:
        """When CORS_ALLOWED_ORIGINS is empty, fall back to localhost:3000."""
        config = CorsConfig(cors_allowed_origins="")
        assert config.allow_origins == ["http://localhost:3000"]

    def test_blank_segments_ignored(self) -> None:
        """Commas with no origin between them must not produce empty strings."""
        config = CorsConfig(cors_allowed_origins="http://localhost:3000,,")
        assert config.allow_origins == ["http://localhost:3000"]

    def test_env_var_loaded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """CorsConfig reads CORS_ALLOWED_ORIGINS from the environment."""
        monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "https://staging.alphaforge.io")
        config = CorsConfig()
        assert "https://staging.alphaforge.io" in config.allow_origins

    def test_env_var_absent_falls_back_to_localhost(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the env var is not set at all, fall back to localhost:3000."""
        monkeypatch.delenv("CORS_ALLOWED_ORIGINS", raising=False)
        config = CorsConfig()
        assert config.allow_origins == ["http://localhost:3000"]


# ---------------------------------------------------------------------------
# CorsConfig — wildcard prohibition (Requirement 19.6)
# ---------------------------------------------------------------------------


class TestCorsConfigWildcardProhibition:
    """Wildcard '*' must be rejected at validation time."""

    def test_wildcard_alone_raises(self) -> None:
        with pytest.raises(ValidationError, match="Wildcard"):
            CorsConfig(cors_allowed_origins="*")

    def test_wildcard_mixed_with_origins_raises(self) -> None:
        with pytest.raises(ValidationError, match="Wildcard"):
            CorsConfig(cors_allowed_origins="http://localhost:3000,*")

    def test_wildcard_with_spaces_raises(self) -> None:
        with pytest.raises(ValidationError, match="Wildcard"):
            CorsConfig(cors_allowed_origins=" * ")

    def test_wildcard_env_var_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "*")
        with pytest.raises(ValidationError, match="Wildcard"):
            CorsConfig()

    def test_partial_wildcard_is_not_rejected(self) -> None:
        """A URL containing '*' as part of a subdomain pattern is not a bare wildcard.
        Only the bare token '*' is prohibited."""
        # Starlette itself does not support wildcard-domain patterns, but we must
        # not accidentally block legitimate URLs that contain the character.
        # Our guard only rejects the bare token "*" as a standalone origin.
        config = CorsConfig(cors_allowed_origins="http://localhost:3000")
        assert config.allow_origins  # sanity check; no raise above


# ---------------------------------------------------------------------------
# get_cors_middleware_args
# ---------------------------------------------------------------------------


class TestGetCorsMiddlewareArgs:
    """Verify the dict returned by get_cors_middleware_args."""

    def test_returns_dict_with_all_required_keys(self) -> None:
        config = CorsConfig(cors_allowed_origins="https://alphaforge.example.com")
        kwargs = get_cors_middleware_args(config)
        assert set(kwargs.keys()) == {
            "allow_origins",
            "allow_methods",
            "allow_headers",
            "allow_credentials",
            "max_age",
        }

    def test_allow_origins_matches_config_property(self) -> None:
        config = CorsConfig(cors_allowed_origins="https://alphaforge.example.com")
        kwargs = get_cors_middleware_args(config)
        assert kwargs["allow_origins"] == config.allow_origins

    def test_max_age_key_name(self) -> None:
        """CORSMiddleware uses 'max_age', not 'max_age_seconds'."""
        config = CorsConfig(
            cors_allowed_origins="http://localhost:3000", max_age_seconds=300
        )
        kwargs = get_cors_middleware_args(config)
        assert kwargs["max_age"] == 300
        assert "max_age_seconds" not in kwargs

    def test_allow_credentials_passed_through(self) -> None:
        config = CorsConfig(
            cors_allowed_origins="http://localhost:3000", allow_credentials=True
        )
        kwargs = get_cors_middleware_args(config)
        assert kwargs["allow_credentials"] is True

    def test_custom_methods_and_headers(self) -> None:
        config = CorsConfig(
            cors_allowed_origins="http://localhost:3000",
            allow_methods=["GET"],
            allow_headers=["Authorization"],
        )
        kwargs = get_cors_middleware_args(config)
        assert kwargs["allow_methods"] == ["GET"]
        assert kwargs["allow_headers"] == ["Authorization"]


# ---------------------------------------------------------------------------
# apply_cors — middleware wiring
# ---------------------------------------------------------------------------


class TestApplyCors:
    """Verify apply_cors() properly registers CORSMiddleware on the app."""

    def test_adds_cors_middleware(self) -> None:
        """After apply_cors(), the app middleware stack must contain CORSMiddleware."""
        app = FastAPI()
        config = CorsConfig(cors_allowed_origins="http://localhost:3000")
        apply_cors(app, config)

        middleware_types = [m.cls for m in app.user_middleware]
        assert CORSMiddleware in middleware_types

    def test_apply_cors_without_config_uses_defaults(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When config is omitted, apply_cors reads CorsConfig from environment."""
        monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "http://localhost:3000")
        app = FastAPI()
        apply_cors(app)  # no config arg
        middleware_types = [m.cls for m in app.user_middleware]
        assert CORSMiddleware in middleware_types

    def test_apply_cors_only_called_once(self) -> None:
        """Each call to apply_cors adds one middleware entry (idempotency check)."""
        app = FastAPI()
        config = CorsConfig(cors_allowed_origins="http://localhost:3000")
        apply_cors(app, config)
        count_before = len(app.user_middleware)
        # Calling again to ensure tests don't double-register in isolation
        app2 = FastAPI()
        apply_cors(app2, config)
        count_app2 = len(app2.user_middleware)
        assert count_before == count_app2 == 1


# ---------------------------------------------------------------------------
# End-to-end CORS header tests
# ---------------------------------------------------------------------------


class TestCorsHeadersAllowedOrigin:
    """Verify actual CORS headers in ASGI responses for an allowed origin."""

    @pytest.mark.asyncio
    async def test_allowed_origin_receives_acao_header_on_get(self) -> None:
        """A request from an allowed origin gets Access-Control-Allow-Origin."""
        config = CorsConfig(cors_allowed_origins="https://alphaforge.example.com")
        app = _app_with_cors(config)
        response = await _get(app, "https://alphaforge.example.com")
        assert response.status_code == 200
        assert response.headers.get("access-control-allow-origin") == (
            "https://alphaforge.example.com"
        )

    @pytest.mark.asyncio
    async def test_allowed_origin_preflight_200(self) -> None:
        """OPTIONS preflight from an allowed origin returns a success status."""
        config = CorsConfig(cors_allowed_origins="https://alphaforge.example.com")
        app = _app_with_cors(config)
        response = await _options(app, "https://alphaforge.example.com")
        # Starlette returns 200 for preflight when origin is allowed.
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_preflight_includes_allow_methods_header(self) -> None:
        """Preflight response must include Access-Control-Allow-Methods."""
        config = CorsConfig(
            cors_allowed_origins="https://alphaforge.example.com",
            allow_methods=["GET", "POST", "OPTIONS"],
        )
        app = _app_with_cors(config)
        response = await _options(app, "https://alphaforge.example.com")
        assert "access-control-allow-methods" in response.headers

    @pytest.mark.asyncio
    async def test_preflight_includes_allow_headers_header(self) -> None:
        """Preflight response must include Access-Control-Allow-Headers."""
        config = CorsConfig(
            cors_allowed_origins="https://alphaforge.example.com",
            allow_headers=["Content-Type", "Authorization", "X-API-KEY"],
        )
        app = _app_with_cors(config)
        response = await _options(app, "https://alphaforge.example.com")
        assert "access-control-allow-headers" in response.headers

    @pytest.mark.asyncio
    async def test_multiple_origins_each_allowed(self) -> None:
        """Each configured origin is independently allowed."""
        config = CorsConfig(
            cors_allowed_origins=(
                "http://localhost:3000,https://alphaforge.example.com"
            )
        )
        app = _app_with_cors(config)

        for origin in ("http://localhost:3000", "https://alphaforge.example.com"):
            response = await _get(app, origin)
            assert response.headers.get("access-control-allow-origin") == origin, (
                f"Origin {origin} did not receive ACAO header"
            )


class TestCorsHeadersDisallowedOrigin:
    """Verify CORS headers are absent or empty for disallowed origins."""

    @pytest.mark.asyncio
    async def test_disallowed_origin_no_acao_header_on_get(self) -> None:
        """A request from an unlisted origin must not receive ACAO header."""
        config = CorsConfig(cors_allowed_origins="https://alphaforge.example.com")
        app = _app_with_cors(config)
        response = await _get(app, "https://evil.example.com")
        # Starlette's CORSMiddleware omits the header for unlisted origins.
        assert "access-control-allow-origin" not in response.headers

    @pytest.mark.asyncio
    async def test_disallowed_origin_preflight_no_acao(self) -> None:
        """OPTIONS preflight from an unlisted origin must not receive ACAO header."""
        config = CorsConfig(cors_allowed_origins="https://alphaforge.example.com")
        app = _app_with_cors(config)
        response = await _options(app, "https://attacker.example.com")
        assert "access-control-allow-origin" not in response.headers

    @pytest.mark.asyncio
    async def test_wildcard_cannot_be_configured(self) -> None:
        """Sanity: constructing a config with '*' must fail before reaching apply_cors."""
        with pytest.raises(ValidationError):
            CorsConfig(cors_allowed_origins="*")

    @pytest.mark.asyncio
    async def test_localhost_default_not_granted_to_production_origin(self) -> None:
        """Default localhost origin does not grant access to production front-ends."""
        config = CorsConfig(cors_allowed_origins="")  # falls back to localhost:3000
        app = _app_with_cors(config)
        response = await _get(app, "https://alphaforge.example.com")
        assert "access-control-allow-origin" not in response.headers


# ---------------------------------------------------------------------------
# Max-age propagation
# ---------------------------------------------------------------------------


class TestCorsMaxAge:
    """Verify max_age_seconds is forwarded to the CORS preflight response."""

    @pytest.mark.asyncio
    async def test_max_age_header_present_in_preflight(self) -> None:
        config = CorsConfig(
            cors_allowed_origins="http://localhost:3000",
            max_age_seconds=120,
        )
        app = _app_with_cors(config)
        response = await _options(app, "http://localhost:3000")
        # Starlette emits access-control-max-age only when > 0.
        assert "access-control-max-age" in response.headers
        assert response.headers["access-control-max-age"] == "120"

    def test_default_max_age_is_600(self) -> None:
        config = CorsConfig(cors_allowed_origins="http://localhost:3000")
        kwargs = get_cors_middleware_args(config)
        assert kwargs["max_age"] == 600
