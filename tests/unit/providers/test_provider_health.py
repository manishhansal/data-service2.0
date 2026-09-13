"""
Unit tests for GET /v1/providers/health — task 4.9.

Covers:
  - Endpoint returns HTTP 200
  - All providers from the Capability_Matrix are present in the response
  - Each entry has the required fields (status, circuitState, availability, …)
  - Credential fields are stripped from the response
  - Circuit state is reflected correctly (OPEN / HALF_OPEN / CLOSED)
  - Falls back gracefully when Redis is unavailable (UNKNOWN/CLOSED defaults)
  - ``_strip_credentials`` removes every credential-like field name

Requirements: 5.11, 16.8, 19.4
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.api.providers import _strip_credentials, router as providers_router
from src.core.schemas.provider import DataType, ProviderId
from src.providers.capability_matrix import _MATRIX


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_app(
    redis_client: Any = None,
    failure_threshold: int = 5,
    recovery_window_sec: int = 60,
) -> FastAPI:
    """Build a minimal FastAPI app for testing the providers endpoint."""
    app = FastAPI()
    app.include_router(providers_router, prefix="/v1", tags=["Providers"])

    # Attach a settings-like object so the endpoint can read thresholds.
    settings = MagicMock()
    settings.circuit_breaker_failure_threshold = failure_threshold
    settings.circuit_breaker_recovery_window_sec = recovery_window_sec
    app.state.settings = settings
    app.state.redis = redis_client
    return app


async def _get(app: FastAPI, path: str) -> Any:
    """Issue a GET request via ASGI transport."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


def _expected_capability_keys() -> set[str]:
    """Return the set of unique '{provider}:{dataType}' keys from the matrix."""
    return {f"{cap.provider.value}:{cap.dataType.value}" for cap in _MATRIX}


# ---------------------------------------------------------------------------
# Basic endpoint behaviour
# ---------------------------------------------------------------------------


class TestProviderHealthEndpoint:
    """GET /v1/providers/health returns 200 with a well-formed response."""

    @pytest.mark.asyncio
    async def test_returns_200(self) -> None:
        app = _make_app()
        response = await _get(app, "/v1/providers/health")
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_response_has_providers_key(self) -> None:
        app = _make_app()
        response = await _get(app, "/v1/providers/health")
        body = response.json()
        assert "providers" in body, "Response must have a 'providers' key"

    @pytest.mark.asyncio
    async def test_providers_is_a_dict(self) -> None:
        app = _make_app()
        body = (await _get(app, "/v1/providers/health")).json()
        assert isinstance(body["providers"], dict)

    @pytest.mark.asyncio
    async def test_all_capability_pairs_present(self) -> None:
        """Every provider × data-type pair from the Capability_Matrix must appear."""
        app = _make_app()
        body = (await _get(app, "/v1/providers/health")).json()
        providers_dict = body["providers"]
        expected = _expected_capability_keys()
        actual = set(providers_dict.keys())
        missing = expected - actual
        assert not missing, (
            f"The following capability pairs are missing from the response: {missing}"
        )

    @pytest.mark.asyncio
    async def test_all_provider_ids_represented(self) -> None:
        """Every ProviderId must appear in at least one key in the response."""
        app = _make_app()
        body = (await _get(app, "/v1/providers/health")).json()
        providers_dict = body["providers"]
        provider_ids_in_response = {key.split(":")[0] for key in providers_dict}
        for provider in ProviderId:
            assert provider.value in provider_ids_in_response, (
                f"ProviderId.{provider.name} ({provider.value}) not represented "
                "in the health response"
            )


# ---------------------------------------------------------------------------
# Required fields on each health entry
# ---------------------------------------------------------------------------


class TestHealthEntryFields:
    """Each provider×capability entry has all required fields."""

    _REQUIRED_FIELDS = {
        "provider",
        "capability",
        "status",
        "circuitState",
        "availability",
        "latencyP50Ms",
        "latencyP99Ms",
        "errorRate",
        "lastSuccessAt",
        "lastFailureReason",
        "semanticIntegrity",
    }

    @pytest.mark.asyncio
    async def test_all_required_fields_present_on_each_entry(self) -> None:
        app = _make_app()
        body = (await _get(app, "/v1/providers/health")).json()
        for key, entry in body["providers"].items():
            missing = self._REQUIRED_FIELDS - set(entry.keys())
            assert not missing, (
                f"Entry '{key}' is missing required fields: {missing}"
            )

    @pytest.mark.asyncio
    async def test_status_is_valid_value(self) -> None:
        valid_statuses = {"UP", "DOWN", "DEGRADED", "UNKNOWN"}
        app = _make_app()
        body = (await _get(app, "/v1/providers/health")).json()
        for key, entry in body["providers"].items():
            assert entry["status"] in valid_statuses, (
                f"Entry '{key}' has invalid status: {entry['status']!r}"
            )

    @pytest.mark.asyncio
    async def test_circuit_state_is_valid_value(self) -> None:
        valid_states = {"CLOSED", "OPEN", "HALF_OPEN"}
        app = _make_app()
        body = (await _get(app, "/v1/providers/health")).json()
        for key, entry in body["providers"].items():
            assert entry["circuitState"] in valid_states, (
                f"Entry '{key}' has invalid circuitState: {entry['circuitState']!r}"
            )

    @pytest.mark.asyncio
    async def test_availability_is_float_between_0_and_1(self) -> None:
        app = _make_app()
        body = (await _get(app, "/v1/providers/health")).json()
        for key, entry in body["providers"].items():
            avail = entry["availability"]
            assert isinstance(avail, (int, float)), (
                f"Entry '{key}' availability must be numeric, got {type(avail)}"
            )
            assert 0.0 <= avail <= 1.0, (
                f"Entry '{key}' availability {avail} is outside [0.0, 1.0]"
            )

    @pytest.mark.asyncio
    async def test_error_rate_is_float_between_0_and_1(self) -> None:
        app = _make_app()
        body = (await _get(app, "/v1/providers/health")).json()
        for key, entry in body["providers"].items():
            rate = entry["errorRate"]
            assert isinstance(rate, (int, float)), (
                f"Entry '{key}' errorRate must be numeric"
            )
            assert 0.0 <= rate <= 1.0, (
                f"Entry '{key}' errorRate {rate} is outside [0.0, 1.0]"
            )

    @pytest.mark.asyncio
    async def test_semantic_integrity_is_bool(self) -> None:
        app = _make_app()
        body = (await _get(app, "/v1/providers/health")).json()
        for key, entry in body["providers"].items():
            assert isinstance(entry["semanticIntegrity"], bool), (
                f"Entry '{key}' semanticIntegrity must be bool"
            )

    @pytest.mark.asyncio
    async def test_provider_field_matches_key_prefix(self) -> None:
        app = _make_app()
        body = (await _get(app, "/v1/providers/health")).json()
        for key, entry in body["providers"].items():
            provider_in_key = key.split(":")[0]
            assert entry["provider"] == provider_in_key, (
                f"Entry key prefix '{provider_in_key}' does not match "
                f"entry['provider'] '{entry['provider']}'"
            )

    @pytest.mark.asyncio
    async def test_capability_field_matches_key_suffix(self) -> None:
        app = _make_app()
        body = (await _get(app, "/v1/providers/health")).json()
        for key, entry in body["providers"].items():
            capability_in_key = key.split(":", 1)[1]
            assert entry["capability"] == capability_in_key, (
                f"Entry key suffix '{capability_in_key}' does not match "
                f"entry['capability'] '{entry['capability']}'"
            )


# ---------------------------------------------------------------------------
# Credential stripping
# ---------------------------------------------------------------------------


class TestCredentialStripping:
    """Credential fields must never appear in the API response."""

    @pytest.mark.asyncio
    async def test_no_key_fields_in_response(self) -> None:
        """Fields containing 'key' (case-insensitive) must be absent."""
        app = _make_app()
        raw = (await _get(app, "/v1/providers/health")).text
        # A naive but effective check: no JSON key containing the pattern
        # should appear in the serialised response body.
        parsed = json.loads(raw)
        _assert_no_credential_fields(parsed)

    @pytest.mark.asyncio
    async def test_no_token_fields_in_response(self) -> None:
        """Fields containing 'token' must be absent."""
        # Inject a fake entry with a token field into the gateway output
        fake_entry = {
            "provider": "angel_one",
            "capability": "HISTORICAL_OHLCV",
            "status": "UNKNOWN",
            "circuitState": "CLOSED",
            "availability": 1.0,
            "latencyP50Ms": None,
            "latencyP99Ms": None,
            "errorRate": 0.0,
            "lastSuccessAt": None,
            "lastFailureReason": None,
            "semanticIntegrity": True,
            "apiToken": "super-secret-value",      # must be stripped
            "authTokenValue": "another-secret",    # must be stripped
        }
        with patch(
            "src.api.providers.ProviderGateway.get_provider_health",
            return_value={"angel_one:HISTORICAL_OHLCV": fake_entry},
        ):
            app = _make_app()
            raw = (await _get(app, "/v1/providers/health")).text
            assert "super-secret-value" not in raw
            assert "another-secret" not in raw
            assert "apiToken" not in raw
            assert "authTokenValue" not in raw

    @pytest.mark.asyncio
    async def test_no_secret_fields_in_response(self) -> None:
        """Fields containing 'secret' must be absent."""
        fake_entry = {
            "provider": "angel_one",
            "capability": "LIVE_QUOTE",
            "status": "UNKNOWN",
            "circuitState": "CLOSED",
            "availability": 1.0,
            "latencyP50Ms": None,
            "latencyP99Ms": None,
            "errorRate": 0.0,
            "lastSuccessAt": None,
            "lastFailureReason": None,
            "semanticIntegrity": True,
            "clientSecret": "my-secret-value",
        }
        with patch(
            "src.api.providers.ProviderGateway.get_provider_health",
            return_value={"angel_one:LIVE_QUOTE": fake_entry},
        ):
            app = _make_app()
            raw = (await _get(app, "/v1/providers/health")).text
            assert "clientSecret" not in raw
            assert "my-secret-value" not in raw

    def test_strip_credentials_removes_key_field(self) -> None:
        data = {"apiKey": "secret", "status": "UP"}
        result = _strip_credentials(data)
        assert "apiKey" not in result
        assert result["status"] == "UP"

    def test_strip_credentials_removes_token_field(self) -> None:
        data = {"authToken": "abc123", "provider": "angel_one"}
        result = _strip_credentials(data)
        assert "authToken" not in result
        assert result["provider"] == "angel_one"

    def test_strip_credentials_removes_secret_field(self) -> None:
        data = {"clientSecret": "xyz", "status": "DOWN"}
        result = _strip_credentials(data)
        assert "clientSecret" not in result

    def test_strip_credentials_removes_password_field(self) -> None:
        data = {"adminPassword": "hunter2", "uptime": 999}
        result = _strip_credentials(data)
        assert "adminPassword" not in result
        assert result["uptime"] == 999

    def test_strip_credentials_removes_credential_field(self) -> None:
        data = {"credential": "value", "other": "keep"}
        result = _strip_credentials(data)
        assert "credential" not in result
        assert result["other"] == "keep"

    def test_strip_credentials_is_case_insensitive(self) -> None:
        data = {"API_KEY": "val1", "AUTH_TOKEN": "val2", "STATUS": "UP"}
        result = _strip_credentials(data)
        assert "API_KEY" not in result
        assert "AUTH_TOKEN" not in result
        assert result["STATUS"] == "UP"

    def test_strip_credentials_handles_nested_dicts(self) -> None:
        data = {
            "providers": {
                "angel_one": {
                    "apiKey": "secret",
                    "status": "UP",
                }
            }
        }
        result = _strip_credentials(data)
        assert "apiKey" not in result["providers"]["angel_one"]
        assert result["providers"]["angel_one"]["status"] == "UP"

    def test_strip_credentials_handles_lists(self) -> None:
        data = [{"apiKey": "s", "status": "UP"}, {"token": "t", "name": "x"}]
        result = _strip_credentials(data)
        assert "apiKey" not in result[0]
        assert result[0]["status"] == "UP"
        assert "token" not in result[1]
        assert result[1]["name"] == "x"

    def test_strip_credentials_preserves_non_credential_fields(self) -> None:
        data = {"status": "UP", "availability": 0.99, "errorRate": 0.01}
        result = _strip_credentials(data)
        assert result == data


# ---------------------------------------------------------------------------
# Circuit state reflection
# ---------------------------------------------------------------------------


class TestCircuitStateReflection:
    """Live circuit-breaker state is reflected in the response."""

    @pytest.mark.asyncio
    async def test_open_circuit_sets_status_to_down(self) -> None:
        """When the circuit is OPEN, status should be set to DOWN."""
        from src.core.schemas.provider import CircuitState  # noqa: PLC0415

        mock_cb = AsyncMock()
        mock_cb.get_state.return_value = CircuitState.OPEN
        mock_cb_instance = mock_cb

        # Patch CircuitBreaker constructor to return our mock.
        with patch(
            "src.api.providers.CircuitBreaker",
            return_value=mock_cb_instance,
        ):
            app = _make_app()
            body = (await _get(app, "/v1/providers/health")).json()
            # Every entry should have circuitState=OPEN and status=DOWN
            # because we patched all CircuitBreaker instances.
            for key, entry in body["providers"].items():
                assert entry["circuitState"] == "OPEN", (
                    f"Entry '{key}' should have circuitState=OPEN"
                )
                assert entry["status"] == "DOWN", (
                    f"Entry '{key}' should have status=DOWN when circuit is OPEN"
                )

    @pytest.mark.asyncio
    async def test_half_open_circuit_sets_status_to_degraded(self) -> None:
        """When the circuit is HALF_OPEN, status should be DEGRADED."""
        from src.core.schemas.provider import CircuitState  # noqa: PLC0415

        mock_cb = AsyncMock()
        mock_cb.get_state.return_value = CircuitState.HALF_OPEN

        with patch(
            "src.api.providers.CircuitBreaker",
            return_value=mock_cb,
        ):
            app = _make_app()
            body = (await _get(app, "/v1/providers/health")).json()
            for key, entry in body["providers"].items():
                assert entry["circuitState"] == "HALF_OPEN", (
                    f"Entry '{key}' should have circuitState=HALF_OPEN"
                )
                assert entry["status"] == "DEGRADED", (
                    f"Entry '{key}' should have status=DEGRADED when circuit is HALF_OPEN"
                )

    @pytest.mark.asyncio
    async def test_closed_circuit_keeps_unknown_status(self) -> None:
        """When the circuit is CLOSED and no live metrics exist, status stays UNKNOWN."""
        from src.core.schemas.provider import CircuitState  # noqa: PLC0415

        mock_cb = AsyncMock()
        mock_cb.get_state.return_value = CircuitState.CLOSED

        with patch(
            "src.api.providers.CircuitBreaker",
            return_value=mock_cb,
        ):
            app = _make_app()
            body = (await _get(app, "/v1/providers/health")).json()
            for key, entry in body["providers"].items():
                assert entry["circuitState"] == "CLOSED", (
                    f"Entry '{key}' should have circuitState=CLOSED"
                )
                # CLOSED + no live metrics → UNKNOWN (not UP, because no metrics yet)
                assert entry["status"] == "UNKNOWN", (
                    f"Entry '{key}' should stay UNKNOWN when circuit is CLOSED "
                    f"and no live metrics are available"
                )

    @pytest.mark.asyncio
    async def test_redis_unavailable_falls_back_to_defaults(self) -> None:
        """When Redis is unavailable, all entries use UNKNOWN/CLOSED defaults."""
        # Pass no redis client (degraded mode).
        app = _make_app(redis_client=None)
        body = (await _get(app, "/v1/providers/health")).json()
        for key, entry in body["providers"].items():
            # Without Redis, the circuit breaker runs in-memory with default CLOSED.
            assert entry["circuitState"] in {"CLOSED", "UNKNOWN"}, (
                f"Entry '{key}' circuitState should be CLOSED or UNKNOWN in degraded mode"
            )

    @pytest.mark.asyncio
    async def test_circuit_breaker_exception_falls_back_gracefully(self) -> None:
        """If circuit breaker raises, the entry keeps safe defaults."""
        with patch(
            "src.api.providers.CircuitBreaker",
            side_effect=Exception("Redis connection refused"),
        ):
            app = _make_app()
            # Should not raise — always returns 200
            response = await _get(app, "/v1/providers/health")
            assert response.status_code == 200
            body = response.json()
            assert "providers" in body


# ---------------------------------------------------------------------------
# No credentials in the actual live response
# ---------------------------------------------------------------------------


def _assert_no_credential_fields(obj: Any, path: str = "") -> None:
    """Recursively assert that no key contains a credential-like substring."""
    import re  # noqa: PLC0415

    _CRED_RE = re.compile(r"key|token|secret|password|credential", re.IGNORECASE)
    if isinstance(obj, dict):
        for k, v in obj.items():
            assert not _CRED_RE.search(k), (
                f"Credential-like field '{k}' found at path '{path}'"
            )
            _assert_no_credential_fields(v, path=f"{path}.{k}")
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            _assert_no_credential_fields(item, path=f"{path}[{i}]")
