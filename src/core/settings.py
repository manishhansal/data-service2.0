"""
Settings management for DATA-SERVICE 2.0.

All tunable parameters are sourced from environment variables — no hardcoded
values (Requirement 20.7).  Uses Pydantic v2 ``BaseSettings``.

Environment file auto-selection
--------------------------------
Set the ``APP_ENV`` environment variable to control which ``.env.*`` file is
loaded at startup:

    APP_ENV=local        loads .env.local  (development defaults)
    APP_ENV=production   loads .env.production
    APP_ENV=staging      loads .env.staging  (falls back to .env if absent)
    (unset / default)    loads .env

This means you never need to juggle ``-e`` flags or rename files — just set
``APP_ENV`` in your shell, Docker Compose ``environment:`` block, or CI/CD
pipeline.

Example::

    # Start with local dev settings
    APP_ENV=local uvicorn src.server:app --reload

    # Docker Compose production
    APP_ENV=production docker compose up -d
"""

from __future__ import annotations

import os
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class OtelExporter(str, Enum):
    """Supported OpenTelemetry span exporters."""

    JAEGER = "jaeger"
    OTLP = "otlp"
    NOOP = "noop"


class LogLevel(str, Enum):
    """Supported log levels."""

    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class SecretsBackend(str, Enum):
    """Supported secrets storage backends."""

    ENV = "env"
    VAULT = "vault"
    AWS_SECRETS = "aws_secrets"


class Environment(str, Enum):
    """Deployment environment."""

    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"


# ---------------------------------------------------------------------------
# Auto env-file resolver
# ---------------------------------------------------------------------------


def _resolve_env_file() -> str:
    """Return the path of the .env file to load based on ``APP_ENV``.

    Priority order (first existing file wins):
        1. ``APP_ENV=local``         → ``.env.local``
        2. ``APP_ENV=production``    → ``.env.production``
        3. ``APP_ENV=staging``       → ``.env.staging``
        4. *(any APP_ENV)*           → ``.env.{APP_ENV}``
        5. Fallback                  → ``.env``

    The function resolves paths relative to the repository root (two levels
    above this file: ``src/core/settings.py`` → ``../../``).

    Returns:
        Absolute path string of the env file to pass to ``SettingsConfigDict``.
    """
    repo_root = Path(__file__).resolve().parent.parent.parent  # src/core/settings.py -> repo root
    app_env = os.environ.get("APP_ENV", "").strip().lower()

    candidates: list[Path] = []
    if app_env:
        candidates.append(repo_root / f".env.{app_env}")
    # Always fall back to the plain .env
    candidates.append(repo_root / ".env")

    for path in candidates:
        if path.exists():
            return str(path)

    # Return the first candidate even if it doesn't exist yet — Pydantic will
    # silently skip a missing env file and rely on environment variables.
    return str(candidates[0])


# ---------------------------------------------------------------------------
# Settings model
# ---------------------------------------------------------------------------


class Settings(BaseSettings):
    """Platform-wide settings loaded from environment variables.

    Every tunable parameter is sourced from an environment variable.
    No tunable parameter is hardcoded (Requirement 20.7).

    The env file loaded is determined automatically by the ``APP_ENV``
    environment variable (see module docstring and ``_resolve_env_file``).
    """

    model_config = SettingsConfigDict(
        env_file=_resolve_env_file(),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── HTTP server ──────────────────────────────────────────────────────
    data_service_port: int = Field(default=8200, ge=1, le=65535)
    uvicorn_workers: int = Field(default=4, ge=1)
    environment: Environment = Environment.DEVELOPMENT

    # ── Redis ────────────────────────────────────────────────────────────
    redis_url: str = "redis://redis:6379/0"

    # ── PostgreSQL ───────────────────────────────────────────────────────
    # Required — no default; must be supplied via DATABASE_URL env var.
    database_url: str

    # ── Circuit breaker ──────────────────────────────────────────────────
    circuit_breaker_failure_threshold: int = Field(default=5, ge=1, le=100)
    circuit_breaker_recovery_window_sec: int = Field(default=60, ge=1, le=3600)

    # ── Cache ────────────────────────────────────────────────────────────
    cache_l1_max_entries: int = Field(default=10_000, ge=1)

    # ── Provider queue ───────────────────────────────────────────────────
    provider_queue_max_depth: int = Field(default=100, ge=1, le=10_000)

    # ── Gap recovery ─────────────────────────────────────────────────────
    gap_recovery_max_attempts: int = Field(default=5, ge=1, le=10)

    # ── Backfill chunk sizes (calendar days) ─────────────────────────────
    # Angel One SmartAPI
    backfill_chunk_angel_1m_days: int = Field(default=30, ge=1)
    # Shared field for 5m and 15m (both capped at 90 days for Angel One)
    backfill_chunk_angel_5m_15m_days: int = Field(default=90, ge=1)
    # Upstox V3
    backfill_chunk_upstox_1m_days: int = Field(default=7, ge=1)
    # Shared field for 5m and 15m (both capped at 30 days for Upstox)
    backfill_chunk_upstox_5m_15m_days: int = Field(default=30, ge=1)
    backfill_chunk_upstox_1d_days: int = Field(default=365, ge=1)

    # ── CORS ─────────────────────────────────────────────────────────────
    # Comma-separated list of allowed origins.  Wildcard (*) is prohibited
    # on production endpoints (Requirement 19.6).
    cors_allowed_origins: str = ""

    # ── Observability ────────────────────────────────────────────────────
    otel_exporter: OtelExporter = OtelExporter.NOOP
    log_level: LogLevel = LogLevel.INFO

    # ── Secrets backend ──────────────────────────────────────────────────
    secrets_backend: SecretsBackend = SecretsBackend.ENV

    # ── Consumer authentication (Requirement 19.2, Task 13.2) ────────────
    # Secret used to sign/verify HS256 JWTs.  Must be at least 32 chars.
    # REQUIRED in production; defaults to a dev-only placeholder.
    jwt_secret: str = "dev-secret-change-in-production-must-be-32-chars"
    # JWT token lifetime in seconds (default 1 hour).
    jwt_expiry_seconds: int = Field(default=3600, ge=60)
    # Comma-separated list of valid API keys for consumer authentication.
    # Example: "key1,key2,key3"
    consumer_api_keys: str = ""

    # ── Provider credentials (optional — fetched from secrets backend) ───
    # These are never included in any API response (Requirement 19.4).
    angel_one_api_key: Optional[str] = None
    angel_one_client_id: Optional[str] = None
    angel_one_totp_secret: Optional[str] = None
    angel_one_mpin: Optional[str] = None  # 4-digit broker login PIN
    upstox_api_key: Optional[str] = None
    upstox_api_secret: Optional[str] = None
    upstox_redirect_uri: Optional[str] = None
    # Pre-obtained OAuth2 access token — set this after completing the OAuth
    # flow externally (e.g. via the Upstox developer console or the
    # /v1/auth/upstox/callback flow).  When present, the adapter skips the
    # authorization-code exchange and uses this token directly.
    upstox_access_token: Optional[str] = None
    # Long-lived analytics JWT issued by Upstox for market-data analytics
    # endpoints (PCR, OI buildup, gainers/losers).  Different from the OAuth
    # access token — does not expire on a per-session basis.
    upstox_analytics_key: Optional[str] = None

    # ── Consumer inbound rate limiting ────────────────────────────────────
    # Controls RateLimitMiddleware (src/middleware/rate_limiter.py).
    # Set CONSUMER_RATE_LIMIT / CONSUMER_RATE_WINDOW_SEC in .env to tune.
    consumer_rate_limit: int = 100      # requests per window
    consumer_rate_window_sec: int = 60  # sliding window duration (seconds)

    # ── Crypto provider base URLs ─────────────────────────────────────────
    # Override these to point at testnet/sandbox environments or a proxy.
    # Defaults are the official public endpoints — no credentials required
    # for public market-data routes (klines, ticker, orderbook, products).

    # --- Binance (spot + futures) ---
    # Public REST: klines, ticker, exchangeInfo, 24hr stats
    binance_rest_base_url: str = "https://api.binance.com"
    # USDM Futures REST: mark price, funding rate, open interest, L/S ratio
    binance_futures_base_url: str = "https://fapi.binance.com"
    # Spot WebSocket: real-time ticks, mini-ticker
    binance_ws_base_url: str = "wss://stream.binance.com:9443"
    # Futures WebSocket: mark price, funding, liquidation streams
    binance_futures_ws_base_url: str = "wss://fstream.binance.com"

    # --- Delta Exchange ---
    # India REST base URL — https://api.india.delta.exchange
    # Use https://api.delta.exchange for global/non-India deployments.
    # Public endpoints (products, orderbook, candles) require NO credentials.
    delta_rest_base_url: str = "https://api.india.delta.exchange"
    # Delta Exchange WebSocket (real-time orderbook, trades, mark price)
    delta_ws_base_url: str = "wss://socket.india.delta.exchange"

    # ── Crypto API credentials (all optional) ─────────────────────────────
    # Public market-data endpoints for both providers work without any key.
    # Only set these if you need authenticated (private) endpoints.

    # Binance API key + secret for private endpoints (e.g., account, orders)
    binance_api_key: Optional[str] = None
    binance_api_secret: Optional[str] = None

    # Delta Exchange API key + secret for private endpoints
    delta_api_key: Optional[str] = None
    delta_api_secret: Optional[str] = None

    # ── Field validators ─────────────────────────────────────────────────

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, v: str) -> str:
        """Require a non-empty PostgreSQL-compatible connection string."""
        if not v:
            raise ValueError("database_url must not be empty")
        allowed_schemes = ("postgresql", "postgres")
        if not any(v.startswith(f"{scheme}") for scheme in allowed_schemes):
            raise ValueError(
                f"database_url scheme must be 'postgresql' or 'postgres'; got: {v!r}"
            )
        return v

    @field_validator("cors_allowed_origins")
    @classmethod
    def validate_cors_no_wildcard(cls, v: str) -> str:
        """Prohibit wildcard CORS origins on all endpoints (Requirement 19.6)."""
        parts = [p.strip() for p in v.split(",") if p.strip()]
        if "*" in parts:
            raise ValueError(
                "Wildcard CORS origin ('*') is prohibited on all endpoints "
                "(Requirement 19.6). Provide explicit allowed origins."
            )
        return v

    # ── Convenience properties ────────────────────────────────────────────

    @property
    def cors_origins_list(self) -> list[str]:
        """Return CORS allowed origins as a list, stripping whitespace."""
        return [o.strip() for o in self.cors_allowed_origins.split(",") if o.strip()]

    @property
    def consumer_api_keys_list(self) -> list[str]:
        """Return valid consumer API keys as a list, stripping whitespace."""
        return [k.strip() for k in self.consumer_api_keys.split(",") if k.strip()]

    @property
    def backfill_chunk_days(self) -> dict[str, int]:
        """Return all backfill chunk sizes as a structured dict."""
        return {
            "angel_one_1m": self.backfill_chunk_angel_1m_days,
            "angel_one_5m": self.backfill_chunk_angel_5m_15m_days,
            "angel_one_15m": self.backfill_chunk_angel_5m_15m_days,
            "upstox_1m": self.backfill_chunk_upstox_1m_days,
            "upstox_5m": self.backfill_chunk_upstox_5m_15m_days,
            "upstox_15m": self.backfill_chunk_upstox_5m_15m_days,
            "upstox_1d": self.backfill_chunk_upstox_1d_days,
        }

    @property
    def active_env_file(self) -> str:
        """Return the path of the env file that was loaded."""
        return _resolve_env_file()


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached singleton Settings instance.

    Importing this function (rather than ``Settings`` directly) allows tests
    to monkeypatch ``src.core.settings.get_settings`` cleanly without
    affecting the global ``settings`` object.
    """
    return Settings()


# Module-level singleton.  Only available when DATABASE_URL is set in the
# environment (raises ValidationError on import otherwise).
# Re-export as ``settings`` for convenience:
#   from src.core.settings import settings
try:
    settings: Settings = get_settings()
except Exception:  # noqa: BLE001
    # During testing or CI, DATABASE_URL may not be set.  Let individual
    # call sites use get_settings() instead.
    pass
