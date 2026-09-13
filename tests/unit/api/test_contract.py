"""
Unit tests for the DataParityContract endpoint.

``GET /v1/contract``   — returns the DataParityContract singleton
``POST /v1/contract``  — HTTP 405 (read-only)
``PUT /v1/contract``   — HTTP 405 (read-only)
``PATCH /v1/contract`` — HTTP 405 (read-only)
``DELETE /v1/contract``— HTTP 405 (read-only)

Tests cover:
    * Response shape and HTTP status codes
    * contractVersion and marketDataApiVersion values
    * supportedMarkets list
    * supportedIntervals for india (no 3m) and crypto (3m allowed)
    * prohibitedIntervals["india"] contains "3m"
    * nullSemantics, oiSourceRule, ivRule are non-empty strings
    * guarantees is a non-empty list of strings
    * generatedAt is a Z-suffixed UTC ISO-8601 string
    * parityVerified defaults to False
    * lastVerifiedAt defaults to null
    * Data path fields are non-empty strings
    * Canonical success envelope shape (data + metadata keys)
    * All mutation verbs return HTTP 405

Requirements: 1.5, 6.2, 6.4, 23.1, 23.2
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

# We import the router and the helpers so we can reset state between tests.
from src.api.contract import (
    build_contract,
    get_current_contract,
    router as contract_router,
    set_parity_verified,
)
from src.api.contract import _contract as _initial_contract  # noqa: PLC2701


# ---------------------------------------------------------------------------
# Test application factory
# ---------------------------------------------------------------------------


def _make_app() -> FastAPI:
    """Build a minimal FastAPI app containing only the contract router."""
    app = FastAPI()
    app.include_router(contract_router, prefix="/v1")
    return app


async def _get(app: FastAPI, path: str, **params: Any) -> Any:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path, params=params)


async def _verb(app: FastAPI, method: str, path: str) -> Any:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        fn = getattr(client, method)
        return await fn(path)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ISO_Z_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


def _assert_iso_z(value: str, field: str) -> None:
    """Assert *value* matches the UTC ISO-8601 Z-suffix pattern."""
    assert isinstance(value, str), f"{field} must be a string"
    assert _ISO_Z_PATTERN.match(value), (
        f"{field}={value!r} does not match UTC ISO-8601 Z-suffix pattern"
    )


# ---------------------------------------------------------------------------
# Schema tests: DataParityContract model (no HTTP)
# ---------------------------------------------------------------------------


class TestDataParityContractSchema:
    """Tests against the Pydantic model directly (no FastAPI layer)."""

    def test_default_contract_has_correct_version(self) -> None:
        contract = build_contract()
        assert contract.contractVersion == "2.0.0"

    def test_default_contract_api_version(self) -> None:
        contract = build_contract()
        assert contract.marketDataApiVersion == "v1"

    def test_supported_markets(self) -> None:
        contract = build_contract()
        assert "india" in contract.supportedMarkets
        assert "crypto" in contract.supportedMarkets

    def test_india_intervals_exclude_3m(self) -> None:
        """3m must never appear in India's supported intervals (Req 1.5, 4.2)."""
        contract = build_contract()
        india_intervals = contract.supportedIntervals["india"]
        assert "3m" not in india_intervals, (
            "3m interval must be permanently excluded from India supported intervals"
        )

    def test_india_intervals_contain_canonical_timeframes(self) -> None:
        contract = build_contract()
        expected = {"1m", "5m", "10m", "15m", "30m", "1h", "1d", "1w", "1M"}
        actual = set(contract.supportedIntervals["india"])
        assert expected.issubset(actual)

    def test_crypto_intervals_allow_3m(self) -> None:
        """Binance natively supports 3m; it is explicitly allowed for crypto."""
        contract = build_contract()
        crypto_intervals = contract.supportedIntervals["crypto"]
        assert "3m" in crypto_intervals, (
            "3m must be present in crypto supported intervals (Binance native)"
        )

    def test_prohibited_intervals_india_contains_3m(self) -> None:
        """prohibitedIntervals['india'] must contain '3m' (Req 1.5)."""
        contract = build_contract()
        assert "3m" in contract.prohibitedIntervals["india"], (
            "3m must be listed in prohibitedIntervals['india']"
        )

    def test_prohibited_intervals_crypto_is_empty(self) -> None:
        contract = build_contract()
        assert contract.prohibitedIntervals["crypto"] == []

    def test_null_semantics_is_non_empty_string(self) -> None:
        contract = build_contract()
        assert isinstance(contract.nullSemantics, str)
        assert len(contract.nullSemantics) > 20

    def test_oi_source_rule_mentions_traded_value(self) -> None:
        """oiSourceRule must explicitly reference the tradedValue prohibition."""
        contract = build_contract()
        rule = contract.oiSourceRule.lower()
        assert "tradedvalue" in rule or "traded_value" in rule or "traded value" in rule

    def test_iv_rule_mentions_null_and_zero(self) -> None:
        """ivRule must mention both 'null' and 'zero' semantics."""
        contract = build_contract()
        rule = contract.ivRule.lower()
        assert "null" in rule
        assert "zero" in rule

    def test_guarantees_is_non_empty_list(self) -> None:
        contract = build_contract()
        assert isinstance(contract.guarantees, list)
        assert len(contract.guarantees) >= 5, "Expected at least 5 guarantee statements"
        for g in contract.guarantees:
            assert isinstance(g, str) and len(g) > 0

    def test_generated_at_is_utc_iso_z(self) -> None:
        contract = build_contract()
        _assert_iso_z(contract.generatedAt, "generatedAt")

    def test_parity_verified_defaults_false(self) -> None:
        """Contract must start as unverified (Req 23.2)."""
        contract = build_contract()
        assert contract.parityVerified is False

    def test_last_verified_at_defaults_null(self) -> None:
        """lastVerifiedAt must be null when never verified (Req 23.2)."""
        contract = build_contract()
        assert contract.lastVerifiedAt is None

    def test_parity_verified_can_be_set_true(self) -> None:
        ts = "2026-01-15T09:00:00.000Z"
        contract = build_contract(parity_verified=True, last_verified_at=ts)
        assert contract.parityVerified is True
        assert contract.lastVerifiedAt == ts

    def test_contract_is_immutable(self) -> None:
        """DataParityContract is frozen — direct field assignment must raise."""
        contract = build_contract()
        with pytest.raises((TypeError, Exception)):
            contract.contractVersion = "9.9.9"  # type: ignore[misc]

    def test_data_paths_are_non_empty_strings(self) -> None:
        contract = build_contract()
        for field in (
            "liveDataPath",
            "paperDataPath",
            "replayDataPath",
            "backtestDataPath",
        ):
            value = getattr(contract, field)
            assert isinstance(value, str) and len(value) > 0, (
                f"{field} must be a non-empty string"
            )

    def test_live_and_paper_paths_are_identical(self) -> None:
        """Paper-trading routes through the same engine as live (Req 23.1)."""
        contract = build_contract()
        assert contract.liveDataPath == contract.paperDataPath, (
            "liveDataPath and paperDataPath must be identical to enforce parity"
        )


# ---------------------------------------------------------------------------
# HTTP endpoint tests
# ---------------------------------------------------------------------------


class TestContractEndpoint:
    """Tests against the FastAPI endpoint via httpx ASGI transport."""

    @pytest.fixture(autouse=True)
    def app(self) -> FastAPI:
        return _make_app()

    # ── GET /v1/contract ──────────────────────────────────────────────────

    async def test_get_contract_returns_200(self, app: FastAPI) -> None:
        resp = await _get(app, "/v1/contract")
        assert resp.status_code == 200

    async def test_get_contract_content_type_is_json(self, app: FastAPI) -> None:
        resp = await _get(app, "/v1/contract")
        assert "application/json" in resp.headers["content-type"]

    async def test_get_contract_has_data_key(self, app: FastAPI) -> None:
        resp = await _get(app, "/v1/contract")
        body = resp.json()
        assert "data" in body, "Response must contain 'data' key (canonical success envelope)"

    async def test_get_contract_has_metadata_key(self, app: FastAPI) -> None:
        resp = await _get(app, "/v1/contract")
        body = resp.json()
        assert "metadata" in body, "Response must contain 'metadata' key"

    async def test_get_contract_metadata_has_required_fields(self, app: FastAPI) -> None:
        resp = await _get(app, "/v1/contract")
        metadata = resp.json()["metadata"]
        assert "requestedAt" in metadata
        assert "dataSourceType" in metadata
        assert metadata["dataSourceType"] == "DERIVED"

    async def test_get_contract_data_has_contract_version(self, app: FastAPI) -> None:
        resp = await _get(app, "/v1/contract")
        data = resp.json()["data"]
        assert data["contractVersion"] == "2.0.0"

    async def test_get_contract_data_has_api_version(self, app: FastAPI) -> None:
        resp = await _get(app, "/v1/contract")
        data = resp.json()["data"]
        assert data["marketDataApiVersion"] == "v1"

    async def test_get_contract_india_intervals_no_3m(self, app: FastAPI) -> None:
        """3m must not appear in India intervals — enforced at the endpoint layer."""
        resp = await _get(app, "/v1/contract")
        india = resp.json()["data"]["supportedIntervals"]["india"]
        assert "3m" not in india, (
            "3m must be absent from India supportedIntervals (Requirement 1.5)"
        )

    async def test_get_contract_crypto_intervals_has_3m(self, app: FastAPI) -> None:
        resp = await _get(app, "/v1/contract")
        crypto = resp.json()["data"]["supportedIntervals"]["crypto"]
        assert "3m" in crypto

    async def test_get_contract_prohibited_intervals_india_has_3m(
        self, app: FastAPI
    ) -> None:
        resp = await _get(app, "/v1/contract")
        prohibited = resp.json()["data"]["prohibitedIntervals"]["india"]
        assert "3m" in prohibited

    async def test_get_contract_parity_verified_false_by_default(
        self, app: FastAPI
    ) -> None:
        resp = await _get(app, "/v1/contract")
        data = resp.json()["data"]
        assert data["parityVerified"] is False

    async def test_get_contract_last_verified_at_null_by_default(
        self, app: FastAPI
    ) -> None:
        resp = await _get(app, "/v1/contract")
        data = resp.json()["data"]
        assert data["lastVerifiedAt"] is None

    async def test_get_contract_generated_at_is_utc_iso_z(self, app: FastAPI) -> None:
        resp = await _get(app, "/v1/contract")
        generated_at = resp.json()["data"]["generatedAt"]
        _assert_iso_z(generated_at, "generatedAt")

    async def test_get_contract_oi_source_rule_present(self, app: FastAPI) -> None:
        resp = await _get(app, "/v1/contract")
        oi_rule = resp.json()["data"]["oiSourceRule"]
        assert isinstance(oi_rule, str) and len(oi_rule) > 0

    async def test_get_contract_iv_rule_present(self, app: FastAPI) -> None:
        resp = await _get(app, "/v1/contract")
        iv_rule = resp.json()["data"]["ivRule"]
        assert isinstance(iv_rule, str) and len(iv_rule) > 0

    async def test_get_contract_null_semantics_present(self, app: FastAPI) -> None:
        resp = await _get(app, "/v1/contract")
        ns = resp.json()["data"]["nullSemantics"]
        assert isinstance(ns, str) and len(ns) > 0

    async def test_get_contract_guarantees_is_list(self, app: FastAPI) -> None:
        resp = await _get(app, "/v1/contract")
        guarantees = resp.json()["data"]["guarantees"]
        assert isinstance(guarantees, list)
        assert len(guarantees) >= 5

    async def test_get_contract_data_paths_present(self, app: FastAPI) -> None:
        resp = await _get(app, "/v1/contract")
        data = resp.json()["data"]
        for field in (
            "liveDataPath",
            "paperDataPath",
            "replayDataPath",
            "backtestDataPath",
        ):
            assert field in data, f"Missing field: {field}"
            assert isinstance(data[field], str) and len(data[field]) > 0

    async def test_get_contract_supported_markets_contains_india_and_crypto(
        self, app: FastAPI
    ) -> None:
        resp = await _get(app, "/v1/contract")
        markets = resp.json()["data"]["supportedMarkets"]
        assert "india" in markets
        assert "crypto" in markets

    # ── Mutation verb tests (all must return 405) ─────────────────────────

    async def test_post_contract_returns_405(self, app: FastAPI) -> None:
        resp = await _verb(app, "post", "/v1/contract")
        assert resp.status_code == 405

    async def test_put_contract_returns_405(self, app: FastAPI) -> None:
        resp = await _verb(app, "put", "/v1/contract")
        assert resp.status_code == 405

    async def test_patch_contract_returns_405(self, app: FastAPI) -> None:
        resp = await _verb(app, "patch", "/v1/contract")
        assert resp.status_code == 405

    async def test_delete_contract_returns_405(self, app: FastAPI) -> None:
        resp = await _verb(app, "delete", "/v1/contract")
        assert resp.status_code == 405

    async def test_mutation_response_uses_error_envelope(self, app: FastAPI) -> None:
        resp = await _verb(app, "post", "/v1/contract")
        body = resp.json()
        assert "error" in body
        assert body["error"]["code"] == "METHOD_NOT_ALLOWED"

    # ── Idempotency ───────────────────────────────────────────────────────

    async def test_get_contract_is_idempotent(self, app: FastAPI) -> None:
        """Two successive GET requests must return the same contract data."""
        resp1 = await _get(app, "/v1/contract")
        resp2 = await _get(app, "/v1/contract")
        # Both must succeed.
        assert resp1.status_code == 200
        assert resp2.status_code == 200
        # Contract data must be identical (not just the same JSON keys).
        d1 = resp1.json()["data"]
        d2 = resp2.json()["data"]
        # Everything except generatedAt and requestedAt (which are module-level
        # constants, so they will actually be the same here too).
        for key in (
            "contractVersion",
            "marketDataApiVersion",
            "supportedMarkets",
            "supportedIntervals",
            "prohibitedIntervals",
            "oiSourceRule",
            "ivRule",
            "parityVerified",
            "lastVerifiedAt",
        ):
            assert d1[key] == d2[key], f"Key '{key}' differs between two GET calls"


# ---------------------------------------------------------------------------
# set_parity_verified helper tests
# ---------------------------------------------------------------------------


class TestSetParityVerified:
    """Tests for the set_parity_verified() module helper."""

    def test_set_parity_verified_updates_contract(self) -> None:
        ts = "2026-06-01T00:00:00.000Z"
        import src.api.contract as _mod  # noqa: PLC0415

        # Save original contract so we can restore it after the test.
        original = _mod._contract

        try:
            set_parity_verified(last_verified_at=ts)
            updated = get_current_contract()
            assert updated.parityVerified is True
            assert updated.lastVerifiedAt == ts
        finally:
            # Restore the original singleton so other tests are not affected.
            _mod._contract = original

    def test_set_parity_verified_updates_generated_at(self) -> None:
        ts = "2026-06-01T00:00:00.000Z"
        import src.api.contract as _mod  # noqa: PLC0415

        original = _mod._contract
        try:
            set_parity_verified(last_verified_at=ts)
            updated = get_current_contract()
            _assert_iso_z(updated.generatedAt, "generatedAt (after set_parity_verified)")
        finally:
            _mod._contract = original

    def test_set_parity_verified_preserves_contract_version(self) -> None:
        import src.api.contract as _mod  # noqa: PLC0415

        original = _mod._contract
        ts = "2026-06-01T00:00:00.000Z"
        try:
            set_parity_verified(last_verified_at=ts)
            updated = get_current_contract()
            assert updated.contractVersion == "2.0.0"
        finally:
            _mod._contract = original
