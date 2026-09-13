"""
CORS allowlist middleware for DATA-SERVICE 2.0.

Implements Requirement 19.6: wildcard CORS (``*``) is prohibited on all
production endpoints.  Only explicitly allowlisted origins may receive CORS
response headers.

Usage::

    from src.middleware.cors_middleware import CorsConfig, apply_cors

    config = CorsConfig()          # reads CORS_ALLOWED_ORIGINS from env
    apply_cors(app, config)        # wires FastAPI's built-in CORSMiddleware
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from fastapi.middleware.cors import CORSMiddleware
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

if TYPE_CHECKING:
    from fastapi import FastAPI


# ---------------------------------------------------------------------------
# Default fallback origins (development only)
# ---------------------------------------------------------------------------

_DEFAULT_DEV_ORIGINS: list[str] = ["http://localhost:3000"]


# ---------------------------------------------------------------------------
# Configuration model
# ---------------------------------------------------------------------------


class CorsConfig(BaseSettings):
    """CORS configuration loaded from environment variables.

    ``CORS_ALLOWED_ORIGINS`` is a comma-separated list of allowed origin
    strings, e.g.::

        CORS_ALLOWED_ORIGINS=http://localhost:3000,https://alphaforge.example.com

    When the variable is absent or empty, the config falls back to
    ``["http://localhost:3000"]`` for developer convenience.  Wildcard ``*``
    is **never** permitted (Requirement 19.6).
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Raw comma-separated string from the environment.
    # The property ``allow_origins`` returns the parsed list.
    cors_allowed_origins: str = Field(
        default="",
        description=(
            "Comma-separated list of allowed CORS origins. "
            "Wildcard '*' is prohibited. "
            "Defaults to http://localhost:3000 when empty."
        ),
    )

    allow_methods: list[str] = Field(
        default=["GET", "POST", "OPTIONS"],
        description="HTTP methods to expose in CORS preflight responses.",
    )

    allow_headers: list[str] = Field(
        default=["Content-Type", "Authorization", "X-API-KEY"],
        description="Request headers to expose in CORS preflight responses.",
    )

    allow_credentials: bool = Field(
        default=False,
        description=(
            "Whether to allow credentials (cookies, auth headers) in cross-origin "
            "requests.  Must be False when allow_origins contains '*' — but wildcard "
            "is already prohibited, so this is informational only."
        ),
    )

    max_age_seconds: int = Field(
        default=600,
        ge=0,
        description="Number of seconds the browser may cache a preflight response.",
    )

    # ── Validators ────────────────────────────────────────────────────────

    @field_validator("cors_allowed_origins")
    @classmethod
    def _no_wildcard(cls, v: str) -> str:
        """Reject any origin list that contains a bare wildcard."""
        parts = [p.strip() for p in v.split(",") if p.strip()]
        if "*" in parts:
            raise ValueError(
                "Wildcard CORS origin ('*') is prohibited on all endpoints "
                "(Requirement 19.6). Supply explicit allowed origins."
            )
        return v

    # ── Properties ────────────────────────────────────────────────────────

    @property
    def allow_origins(self) -> list[str]:
        """Return the parsed list of allowed origins.

        Falls back to ``["http://localhost:3000"]`` when the environment
        variable is absent or blank.
        """
        parsed = [o.strip() for o in self.cors_allowed_origins.split(",") if o.strip()]
        return parsed if parsed else _DEFAULT_DEV_ORIGINS


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def get_cors_middleware_args(config: CorsConfig) -> dict:
    """Return kwargs suitable for ``app.add_middleware(CORSMiddleware, **kwargs)``.

    Args:
        config: A populated :class:`CorsConfig` instance.

    Returns:
        A dict with keys ``allow_origins``, ``allow_methods``,
        ``allow_headers``, ``allow_credentials``, and ``max_age``.
    """
    return {
        "allow_origins": config.allow_origins,
        "allow_methods": config.allow_methods,
        "allow_headers": config.allow_headers,
        "allow_credentials": config.allow_credentials,
        "max_age": config.max_age_seconds,
    }


def apply_cors(app: "FastAPI", config: CorsConfig | None = None) -> None:
    """Add FastAPI's built-in :class:`CORSMiddleware` to *app*.

    This is the single place where CORS middleware is wired.  Calling it
    ensures the wildcard-free ``CorsConfig`` is always used, preventing any
    bypass of the Requirement 19.6 guard.

    Args:
        app:    The :class:`fastapi.FastAPI` application instance.
        config: Optional :class:`CorsConfig`.  A new one is created from
                environment variables when not supplied.
    """
    if config is None:
        config = CorsConfig()

    kwargs = get_cors_middleware_args(config)
    app.add_middleware(CORSMiddleware, **kwargs)
