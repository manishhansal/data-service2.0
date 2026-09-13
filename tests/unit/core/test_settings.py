"""
tests/unit/core/test_settings.py

Unit tests for src/core/settings.py.

Covers:
- All default values (via lowercase field names)
- Mandatory field validation (database_url)
- Range validation for numeric fields
- Enum field acceptance and rejection
- CORS wildcard prohibition
- cors_origins_list helper
- backfill_chunk_days helper
- database_url scheme validation
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

VALID_DB_URL = "postgresql+asyncpg://user:pass@localhost:5432/mds"


def make_settings(**overrides):
    """
    Instantiate Settings with a valid database_url baseline.
    Keyword args map directly to pydantic field names (lowercase).
    """
    from src.core.settings import Settings  # local import so monkeypatching works

    kwargs = {"database_url": VALID_DB_URL}
    kwargs.update(overrides)
    return Settings(**kwargs)


# ---------------------------------------------------------------------------
# Default values
# ---------------------------------------------------------------------------

class TestDefaults:
    def test_port_default(self):
        s = make_settings()
        assert s.data_service_port == 8200

    def test_redis_url_default(self):
        s = make_settings()
        assert s.redis_url == "redis://redis:6379/0"

    def test_circuit_breaker_failure_threshold_default(self):
        s = make_settings()
        assert s.circuit_breaker_failure_threshold == 5

    def test_circuit_breaker_recovery_window_default(self):
        s = make_settings()
        assert s.circuit_breaker_recovery_window_sec == 60

    def test_cache_l1_max_entries_default(self):
        s = make_settings()
        assert s.cache_l1_max_entries == 10_000

    def test_provider_queue_max_depth_default(self):
        s = make_settings()
        assert s.provider_queue_max_depth == 100

    def test_gap_recovery_max_attempts_default(self):
        s = make_settings()
        assert s.gap_recovery_max_attempts == 5

    def test_backfill_angel_1m_default(self):
        s = make_settings()
        assert s.backfill_chunk_angel_1m_days == 30

    def test_backfill_angel_5m_15m_default(self):
        s = make_settings()
        assert s.backfill_chunk_angel_5m_15m_days == 90

    def test_backfill_upstox_1m_default(self):
        s = make_settings()
        assert s.backfill_chunk_upstox_1m_days == 7

    def test_backfill_upstox_5m_15m_default(self):
        s = make_settings()
        assert s.backfill_chunk_upstox_5m_15m_days == 30

    def test_backfill_upstox_1d_default(self):
        s = make_settings()
        assert s.backfill_chunk_upstox_1d_days == 365

    def test_otel_exporter_default(self):
        from src.core.settings import OtelExporter
        s = make_settings()
        assert s.otel_exporter == OtelExporter.NOOP

    def test_log_level_default(self):
        from src.core.settings import LogLevel
        s = make_settings()
        assert s.log_level == LogLevel.INFO

    def test_secrets_backend_default(self):
        from src.core.settings import SecretsBackend
        s = make_settings()
        assert s.secrets_backend == SecretsBackend.ENV

    def test_cors_default_empty(self):
        s = make_settings()
        assert s.cors_allowed_origins == ""

    def test_optional_provider_secrets_default_none(self):
        s = make_settings()
        assert s.angel_one_api_key is None
        assert s.angel_one_client_id is None
        assert s.angel_one_totp_secret is None
        assert s.upstox_api_key is None
        assert s.upstox_api_secret is None
        assert s.upstox_redirect_uri is None


# ---------------------------------------------------------------------------
# DATABASE_URL validation
# ---------------------------------------------------------------------------

class TestDatabaseUrl:
    def test_missing_database_url_raises(self, monkeypatch):
        from src.core.settings import Settings
        monkeypatch.delenv("DATABASE_URL", raising=False)
        with pytest.raises(ValidationError):
            Settings()  # no database_url at all

    def test_empty_database_url_raises(self):
        from src.core.settings import Settings
        with pytest.raises(ValidationError):
            Settings(database_url="")

    def test_postgresql_asyncpg_scheme_accepted(self):
        s = make_settings(database_url="postgresql+asyncpg://u:p@host:5432/db")
        assert "asyncpg" in s.database_url

    def test_postgresql_scheme_accepted(self):
        s = make_settings(database_url="postgresql://u:p@host:5432/db")
        assert s.database_url.startswith("postgresql://")

    def test_postgres_scheme_accepted(self):
        s = make_settings(database_url="postgres://u:p@host:5432/db")
        assert s.database_url.startswith("postgres://")

    def test_mysql_scheme_rejected(self):
        from src.core.settings import Settings
        with pytest.raises(ValidationError):
            Settings(database_url="mysql://u:p@host:3306/db")

    def test_sqlite_scheme_rejected(self):
        from src.core.settings import Settings
        with pytest.raises(ValidationError):
            Settings(database_url="sqlite:///./test.db")


# ---------------------------------------------------------------------------
# Range / value validators
# ---------------------------------------------------------------------------

class TestRangeValidation:
    def test_port_below_range(self):
        with pytest.raises(ValidationError):
            make_settings(data_service_port=0)

    def test_port_above_range(self):
        with pytest.raises(ValidationError):
            make_settings(data_service_port=65536)

    def test_port_valid_boundary_hi(self):
        s = make_settings(data_service_port=65535)
        assert s.data_service_port == 65535

    def test_port_valid_boundary_lo(self):
        s = make_settings(data_service_port=1)
        assert s.data_service_port == 1

    def test_cb_failure_threshold_min(self):
        s = make_settings(circuit_breaker_failure_threshold=1)
        assert s.circuit_breaker_failure_threshold == 1

    def test_cb_failure_threshold_max(self):
        s = make_settings(circuit_breaker_failure_threshold=100)
        assert s.circuit_breaker_failure_threshold == 100

    def test_cb_failure_threshold_zero_rejected(self):
        with pytest.raises(ValidationError):
            make_settings(circuit_breaker_failure_threshold=0)

    def test_cb_failure_threshold_above_max_rejected(self):
        with pytest.raises(ValidationError):
            make_settings(circuit_breaker_failure_threshold=101)

    def test_cb_recovery_window_min(self):
        s = make_settings(circuit_breaker_recovery_window_sec=1)
        assert s.circuit_breaker_recovery_window_sec == 1

    def test_cb_recovery_window_max(self):
        s = make_settings(circuit_breaker_recovery_window_sec=3600)
        assert s.circuit_breaker_recovery_window_sec == 3600

    def test_cb_recovery_window_above_max_rejected(self):
        with pytest.raises(ValidationError):
            make_settings(circuit_breaker_recovery_window_sec=3601)

    def test_cb_recovery_window_zero_rejected(self):
        with pytest.raises(ValidationError):
            make_settings(circuit_breaker_recovery_window_sec=0)

    def test_provider_queue_max_depth_min(self):
        s = make_settings(provider_queue_max_depth=1)
        assert s.provider_queue_max_depth == 1

    def test_provider_queue_max_depth_max(self):
        s = make_settings(provider_queue_max_depth=10_000)
        assert s.provider_queue_max_depth == 10_000

    def test_provider_queue_max_depth_above_max_rejected(self):
        with pytest.raises(ValidationError):
            make_settings(provider_queue_max_depth=10_001)

    def test_provider_queue_max_depth_zero_rejected(self):
        with pytest.raises(ValidationError):
            make_settings(provider_queue_max_depth=0)

    def test_gap_recovery_max_attempts_min(self):
        s = make_settings(gap_recovery_max_attempts=1)
        assert s.gap_recovery_max_attempts == 1

    def test_gap_recovery_max_attempts_max(self):
        s = make_settings(gap_recovery_max_attempts=10)
        assert s.gap_recovery_max_attempts == 10

    def test_gap_recovery_max_attempts_above_max_rejected(self):
        with pytest.raises(ValidationError):
            make_settings(gap_recovery_max_attempts=11)

    def test_gap_recovery_max_attempts_zero_rejected(self):
        with pytest.raises(ValidationError):
            make_settings(gap_recovery_max_attempts=0)


# ---------------------------------------------------------------------------
# Enum settings
# ---------------------------------------------------------------------------

class TestEnumSettings:
    def test_otel_jaeger_accepted(self):
        from src.core.settings import OtelExporter
        s = make_settings(otel_exporter="jaeger")
        assert s.otel_exporter == OtelExporter.JAEGER

    def test_otel_otlp_accepted(self):
        from src.core.settings import OtelExporter
        s = make_settings(otel_exporter="otlp")
        assert s.otel_exporter == OtelExporter.OTLP

    def test_otel_noop_accepted(self):
        from src.core.settings import OtelExporter
        s = make_settings(otel_exporter="noop")
        assert s.otel_exporter == OtelExporter.NOOP

    def test_otel_invalid_value_rejected(self):
        with pytest.raises(ValidationError):
            make_settings(otel_exporter="datadog")

    def test_log_level_debug(self):
        from src.core.settings import LogLevel
        s = make_settings(log_level="DEBUG")
        assert s.log_level == LogLevel.DEBUG

    def test_log_level_warning(self):
        from src.core.settings import LogLevel
        s = make_settings(log_level="WARNING")
        assert s.log_level == LogLevel.WARNING

    def test_log_level_error(self):
        from src.core.settings import LogLevel
        s = make_settings(log_level="ERROR")
        assert s.log_level == LogLevel.ERROR

    def test_log_level_invalid_rejected(self):
        with pytest.raises(ValidationError):
            make_settings(log_level="VERBOSE")

    def test_secrets_backend_vault(self):
        from src.core.settings import SecretsBackend
        s = make_settings(secrets_backend="vault")
        assert s.secrets_backend == SecretsBackend.VAULT

    def test_secrets_backend_aws(self):
        from src.core.settings import SecretsBackend
        s = make_settings(secrets_backend="aws_secrets")
        assert s.secrets_backend == SecretsBackend.AWS_SECRETS

    def test_secrets_backend_invalid_rejected(self):
        with pytest.raises(ValidationError):
            make_settings(secrets_backend="gcp_secrets")


# ---------------------------------------------------------------------------
# CORS validation
# ---------------------------------------------------------------------------

class TestCorsValidation:
    def test_wildcard_origin_rejected(self):
        with pytest.raises(ValidationError, match="[Ww]ildcard"):
            make_settings(cors_allowed_origins="*")

    def test_wildcard_in_list_rejected(self):
        with pytest.raises(ValidationError, match="[Ww]ildcard"):
            make_settings(cors_allowed_origins="https://app.example.com,*")

    def test_valid_origins_accepted(self):
        s = make_settings(cors_allowed_origins="https://app.example.com,https://admin.example.com")
        assert "https://app.example.com" in s.cors_allowed_origins

    def test_empty_cors_accepted(self):
        s = make_settings(cors_allowed_origins="")
        assert s.cors_allowed_origins == ""

    def test_cors_origins_list_helper_empty(self):
        s = make_settings()
        assert s.cors_origins_list == []

    def test_cors_origins_list_helper_single(self):
        s = make_settings(cors_allowed_origins="https://app.example.com")
        assert s.cors_origins_list == ["https://app.example.com"]

    def test_cors_origins_list_helper_multiple(self):
        s = make_settings(cors_allowed_origins="https://a.com,https://b.com")
        assert s.cors_origins_list == ["https://a.com", "https://b.com"]

    def test_cors_origins_list_strips_whitespace(self):
        s = make_settings(cors_allowed_origins="  https://a.com ,  https://b.com  ")
        assert s.cors_origins_list == ["https://a.com", "https://b.com"]


# ---------------------------------------------------------------------------
# Convenience helpers
# ---------------------------------------------------------------------------

class TestConvenienceHelpers:
    def test_backfill_chunk_days_keys_present(self):
        s = make_settings()
        chunks = s.backfill_chunk_days
        expected_keys = {
            "angel_one_1m", "angel_one_5m", "angel_one_15m",
            "upstox_1m", "upstox_5m", "upstox_15m", "upstox_1d",
        }
        assert set(chunks.keys()) == expected_keys

    def test_backfill_chunk_days_default_values(self):
        s = make_settings()
        chunks = s.backfill_chunk_days
        assert chunks["angel_one_1m"] == 30
        assert chunks["angel_one_5m"] == 90
        assert chunks["angel_one_15m"] == 90
        assert chunks["upstox_1m"] == 7
        assert chunks["upstox_5m"] == 30
        assert chunks["upstox_15m"] == 30
        assert chunks["upstox_1d"] == 365

    def test_backfill_chunk_days_reflects_overrides(self):
        s = make_settings(
            backfill_chunk_angel_1m_days=14,
            backfill_chunk_upstox_1d_days=180,
        )
        assert s.backfill_chunk_days["angel_one_1m"] == 14
        assert s.backfill_chunk_days["upstox_1d"] == 180

    def test_get_settings_returns_settings_instance(self):
        from src.core.settings import get_settings, Settings
        assert isinstance(get_settings(), Settings)

    def test_settings_singleton_exported(self):
        """Module-level ``settings`` attribute must be importable when DATABASE_URL is set."""
        import importlib, src.core.settings as m
        # The singleton is already loaded by the time we get here (env var set by pytest runner).
        assert isinstance(m.settings, m.Settings)
