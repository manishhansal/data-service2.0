"""
Unit tests for quality API endpoints (Task 9.7).

Tests cover:
    POST /v1/quality/evaluate  — DataQualityGate + QualityClassification
    GET  /v1/quality/score     — DataConfidenceScore computation

Requirements: 7.1, 7.2, 7.5, 7.6, 7.7, 7.9, 7.11

Tests use an in-process ASGI test client (``httpx.AsyncClient`` via
``ASGITransport``) so no live Redis or PostgreSQL is required.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.api.quality import router as quality_router


# ---------------------------------------------------------------------------
# Test application factory
# ---------------------------------------------------------------------------


def _make_app() -> FastAPI:
    """Build a minimal FastAPI app with only the quality router."""
    app = FastAPI()
    app.include_router(quality_router, prefix="/v1")
    return app


async def _post(app: FastAPI, path: str, json: Any) -> Any:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(path, json=json)


async def _get(app: FastAPI, path: str, **params: Any) -> Any:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path, params=params)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_ms() -> int:
    return int(time.time() * 1000)


def _valid_observation(**overrides: Any) -> dict[str, Any]:
    """Build a minimal observation dict that passes all five gate conditions."""
    base: dict[str, Any] = {
        "symbol": "RELIANCE",
        "timestamp": _now_ms(),
        "open": 2450.0,
        "high": 2460.0,
        "low": 2440.0,
        "close": 2455.0,
        "volume": 1_000_000,
        "source": "angel_one",
        "confidenceScore": 85,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# POST /v1/quality/evaluate — success cases
# ---------------------------------------------------------------------------


class TestEvaluateQualityGateSuccess:
    """Happy-path tests for POST /v1/quality/evaluate."""

    @pytest.mark.asyncio
    async def test_returns_200_for_valid_observation(self) -> None:
        app = _make_app()
        resp = await _post(app, "/v1/quality/evaluate", json=_valid_observation())

        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_response_envelope_structure(self) -> None:
        app = _make_app()
        resp = await _post(app, "/v1/quality/evaluate", json=_valid_observation())
        body = resp.json()

        assert "data" in body
        assert "metadata" in body
        assert "requestedAt" in body["metadata"]
        assert body["metadata"]["dataSourceType"] == "DERIVED"

    @pytest.mark.asyncio
    async def test_data_contains_gate_classification_and_top_level_flag(self) -> None:
        app = _make_app()
        resp = await _post(app, "/v1/quality/evaluate", json=_valid_observation())
        data = resp.json()["data"]

        assert "gate" in data
        assert "classification" in data
        assert "signalEngineAllowed" in data

    @pytest.mark.asyncio
    async def test_gate_has_five_conditions(self) -> None:
        app = _make_app()
        resp = await _post(app, "/v1/quality/evaluate", json=_valid_observation())
        gate = resp.json()["data"]["gate"]

        for field in (
            "dataFresh",
            "dataComplete",
            "dataTimestampValid",
            "dataProviderHealthy",
            "dataSemanticallyValid",
        ):
            assert field in gate, f"Expected '{field}' in gate response"

    @pytest.mark.asyncio
    async def test_gate_has_signal_engine_allowed_and_confidence_score(self) -> None:
        app = _make_app()
        resp = await _post(app, "/v1/quality/evaluate", json=_valid_observation())
        gate = resp.json()["data"]["gate"]

        assert "signalEngineAllowed" in gate
        assert "confidenceScore" in gate
        assert isinstance(gate["confidenceScore"], int)
        assert 0 <= gate["confidenceScore"] <= 95

    @pytest.mark.asyncio
    async def test_classification_has_grade_signal_score_and_reasons(self) -> None:
        app = _make_app()
        resp = await _post(app, "/v1/quality/evaluate", json=_valid_observation())
        cls_ = resp.json()["data"]["classification"]

        assert "grade" in cls_
        assert "signalEngineAllowed" in cls_
        assert "score" in cls_
        assert "reasons" in cls_
        assert isinstance(cls_["reasons"], list)

    @pytest.mark.asyncio
    async def test_all_conditions_pass_for_healthy_observation(self) -> None:
        app = _make_app()
        resp = await _post(app, "/v1/quality/evaluate", json=_valid_observation())
        data = resp.json()["data"]
        gate = data["gate"]

        assert gate["dataFresh"] is True
        assert gate["dataComplete"] is True
        assert gate["dataTimestampValid"] is True
        assert gate["dataProviderHealthy"] is True
        assert gate["dataSemanticallyValid"] is True
        assert gate["signalEngineAllowed"] is True
        assert data["signalEngineAllowed"] is True

    @pytest.mark.asyncio
    async def test_top_level_flag_matches_gate_flag(self) -> None:
        app = _make_app()
        resp = await _post(app, "/v1/quality/evaluate", json=_valid_observation())
        data = resp.json()["data"]

        assert data["signalEngineAllowed"] == data["gate"]["signalEngineAllowed"]

    @pytest.mark.asyncio
    async def test_grade_high_for_high_confidence_observation(self) -> None:
        app = _make_app()
        obs = _valid_observation(confidenceScore=85)
        resp = await _post(app, "/v1/quality/evaluate", json=obs)
        cls_ = resp.json()["data"]["classification"]

        assert cls_["grade"] == "HIGH"

    @pytest.mark.asyncio
    async def test_block_reasons_empty_when_all_pass(self) -> None:
        app = _make_app()
        resp = await _post(app, "/v1/quality/evaluate", json=_valid_observation())
        gate = resp.json()["data"]["gate"]

        assert gate["blockReasons"] == []

    @pytest.mark.asyncio
    async def test_custom_min_confidence_score_query_param(self) -> None:
        """min_confidence_score=30 means score=35 should pass."""
        app = _make_app()
        obs = _valid_observation(confidenceScore=35)
        resp = await _post(
            app,
            "/v1/quality/evaluate?min_confidence_score=30",
            json=obs,
        )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# POST /v1/quality/evaluate — gate failure cases
# ---------------------------------------------------------------------------


class TestEvaluateQualityGateFailures:
    """Tests for observations that should fail one or more gate conditions."""

    @pytest.mark.asyncio
    async def test_incomplete_observation_fails_completeness_gate(self) -> None:
        app = _make_app()
        # Missing 'volume' — required field
        obs = {
            "symbol": "NIFTY",
            "timestamp": _now_ms(),
            "open": 22000.0,
            "high": 22100.0,
            "low": 21900.0,
            "close": 22050.0,
            # volume absent
            "source": "angel_one",
            "confidenceScore": 85,
        }
        resp = await _post(app, "/v1/quality/evaluate", json=obs)
        assert resp.status_code == 200
        gate = resp.json()["data"]["gate"]
        assert gate["dataComplete"] is False
        assert gate["signalEngineAllowed"] is False

    @pytest.mark.asyncio
    async def test_stale_observation_fails_freshness_gate(self) -> None:
        app = _make_app()
        # Timestamp 31 days ago → older than 30-day freshness window
        thirty_one_days_ms = 31 * 24 * 60 * 60 * 1000
        old_ts = _now_ms() - thirty_one_days_ms
        obs = _valid_observation(timestamp=old_ts, eventTimeMs=old_ts)
        resp = await _post(app, "/v1/quality/evaluate", json=obs)
        assert resp.status_code == 200
        gate = resp.json()["data"]["gate"]
        assert gate["dataFresh"] is False
        assert gate["signalEngineAllowed"] is False

    @pytest.mark.asyncio
    async def test_no_source_fails_provider_healthy_gate(self) -> None:
        app = _make_app()
        obs = _valid_observation()
        obs.pop("source", None)
        obs["providerAvailable"] = False
        resp = await _post(app, "/v1/quality/evaluate", json=obs)
        assert resp.status_code == 200
        gate = resp.json()["data"]["gate"]
        assert gate["dataProviderHealthy"] is False

    @pytest.mark.asyncio
    async def test_low_confidence_score_fails_semantic_gate(self) -> None:
        app = _make_app()
        obs = _valid_observation(confidenceScore=20)  # below 30 hard-block
        resp = await _post(app, "/v1/quality/evaluate", json=obs)
        assert resp.status_code == 200
        gate = resp.json()["data"]["gate"]
        assert gate["dataSemanticallyValid"] is False
        assert gate["signalEngineAllowed"] is False

    @pytest.mark.asyncio
    async def test_ohlcv_invariant_violation_fails_timestamp_valid_gate(self) -> None:
        app = _make_app()
        # high < close violates high >= max(open, close)
        obs = _valid_observation(open=100.0, high=90.0, low=80.0, close=95.0)
        resp = await _post(app, "/v1/quality/evaluate", json=obs)
        assert resp.status_code == 200
        gate = resp.json()["data"]["gate"]
        assert gate["dataTimestampValid"] is False

    @pytest.mark.asyncio
    async def test_block_reasons_populated_when_gate_fails(self) -> None:
        app = _make_app()
        obs = _valid_observation(confidenceScore=10)
        resp = await _post(app, "/v1/quality/evaluate", json=obs)
        gate = resp.json()["data"]["gate"]
        assert len(gate["blockReasons"]) >= 1

    @pytest.mark.asyncio
    async def test_blocked_score_sets_signal_engine_allowed_false(self) -> None:
        """Requirement 7.11: score < 30 → signalEngineAllowed always False."""
        app = _make_app()
        obs = _valid_observation(confidenceScore=5)
        resp = await _post(app, "/v1/quality/evaluate", json=obs)
        data = resp.json()["data"]
        assert data["signalEngineAllowed"] is False
        assert data["classification"]["signalEngineAllowed"] is False

    @pytest.mark.asyncio
    async def test_classification_grade_blocked_for_very_low_score(self) -> None:
        app = _make_app()
        obs = _valid_observation(confidenceScore=5)
        resp = await _post(app, "/v1/quality/evaluate", json=obs)
        cls_ = resp.json()["data"]["classification"]
        assert cls_["grade"] == "BLOCKED"


# ---------------------------------------------------------------------------
# POST /v1/quality/evaluate — edge and error cases
# ---------------------------------------------------------------------------


class TestEvaluateQualityGateEdgeCases:
    """Edge cases and error-path tests for POST /v1/quality/evaluate."""

    @pytest.mark.asyncio
    async def test_empty_body_returns_200_with_failed_gates(self) -> None:
        """An empty dict is valid JSON; most gates will fail."""
        app = _make_app()
        resp = await _post(app, "/v1/quality/evaluate", json={})
        assert resp.status_code == 200
        gate = resp.json()["data"]["gate"]
        assert gate["signalEngineAllowed"] is False

    @pytest.mark.asyncio
    async def test_min_confidence_score_below_30_is_rejected(self) -> None:
        """min_confidence_score must be >= 30 (FastAPI query validation)."""
        app = _make_app()
        resp = await _post(
            app,
            "/v1/quality/evaluate?min_confidence_score=10",
            json=_valid_observation(),
        )
        assert resp.status_code == 422  # FastAPI validation error

    @pytest.mark.asyncio
    async def test_min_confidence_score_above_95_is_rejected(self) -> None:
        app = _make_app()
        resp = await _post(
            app,
            "/v1/quality/evaluate?min_confidence_score=100",
            json=_valid_observation(),
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_response_requestedAt_is_utc_iso8601(self) -> None:
        app = _make_app()
        resp = await _post(app, "/v1/quality/evaluate", json=_valid_observation())
        requested_at = resp.json()["metadata"]["requestedAt"]
        # Must end with Z (UTC)
        assert requested_at.endswith("Z")

    @pytest.mark.asyncio
    async def test_extra_fields_in_body_are_ignored(self) -> None:
        """Unknown fields in the observation dict must not cause errors."""
        app = _make_app()
        obs = _valid_observation(
            unknownField="ignored",
            anotherUnknown=42,
        )
        resp = await _post(app, "/v1/quality/evaluate", json=obs)
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# GET /v1/quality/score — success cases
# ---------------------------------------------------------------------------


class TestGetQualityScore:
    """Tests for GET /v1/quality/score."""

    @pytest.mark.asyncio
    async def test_returns_200_with_all_required_params(self) -> None:
        app = _make_app()
        resp = await _get(
            app,
            "/v1/quality/score",
            freshness="FRESH",
            completeness=100.0,
            provider_healthy="true",
            timestamp_valid="true",
            agreement=1.0,
        )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_response_envelope_structure(self) -> None:
        app = _make_app()
        resp = await _get(
            app,
            "/v1/quality/score",
            freshness="FRESH",
            completeness=100.0,
            provider_healthy="true",
            timestamp_valid="true",
            agreement=1.0,
        )
        body = resp.json()
        assert "data" in body
        assert "metadata" in body
        assert body["metadata"]["dataSourceType"] == "DERIVED"

    @pytest.mark.asyncio
    async def test_data_has_score_grade_signal_and_components(self) -> None:
        app = _make_app()
        resp = await _get(
            app,
            "/v1/quality/score",
            freshness="FRESH",
            completeness=100.0,
            provider_healthy="true",
            timestamp_valid="true",
            agreement=1.0,
        )
        data = resp.json()["data"]
        assert "score" in data
        assert "grade" in data
        assert "signalEngineAllowed" in data
        assert "components" in data

    @pytest.mark.asyncio
    async def test_score_is_integer_in_range_0_95(self) -> None:
        app = _make_app()
        resp = await _get(
            app,
            "/v1/quality/score",
            freshness="FRESH",
            completeness=100.0,
            provider_healthy="true",
            timestamp_valid="true",
            agreement=1.0,
        )
        score = resp.json()["data"]["score"]
        assert isinstance(score, int)
        assert 0 <= score <= 95

    @pytest.mark.asyncio
    async def test_maximum_inputs_yield_score_95(self) -> None:
        """35 + 25 + 20 + 10 + 10 = 100 → capped at 95."""
        app = _make_app()
        resp = await _get(
            app,
            "/v1/quality/score",
            freshness="FRESH",
            completeness=100.0,
            provider_healthy="true",
            timestamp_valid="true",
            agreement=1.0,
        )
        score = resp.json()["data"]["score"]
        assert score == 95  # never 100

    @pytest.mark.asyncio
    async def test_zero_inputs_yield_score_0(self) -> None:
        """All-zero inputs: 0 + 0 + 0 + 0 + 0 = 0."""
        app = _make_app()
        resp = await _get(
            app,
            "/v1/quality/score",
            freshness="EXPIRED",
            completeness=0.0,
            provider_healthy="false",
            timestamp_valid="false",
            agreement=0.0,
        )
        score = resp.json()["data"]["score"]
        assert score == 0

    @pytest.mark.asyncio
    async def test_sequence_penalty_reduces_score(self) -> None:
        """sequence_ok=False applies 20% penalty."""
        app = _make_app()
        # First get score without penalty
        resp_ok = await _get(
            app,
            "/v1/quality/score",
            freshness="FRESH",
            completeness=100.0,
            provider_healthy="true",
            timestamp_valid="true",
            agreement=1.0,
            sequence_ok="true",
        )
        resp_penalty = await _get(
            app,
            "/v1/quality/score",
            freshness="FRESH",
            completeness=100.0,
            provider_healthy="true",
            timestamp_valid="true",
            agreement=1.0,
            sequence_ok="false",
        )
        score_ok = resp_ok.json()["data"]["score"]
        score_penalty = resp_penalty.json()["data"]["score"]
        assert score_penalty < score_ok

    @pytest.mark.asyncio
    async def test_sequence_ok_defaults_to_true(self) -> None:
        """Omitting sequence_ok should default to True (no penalty)."""
        app = _make_app()
        resp_default = await _get(
            app,
            "/v1/quality/score",
            freshness="FRESH",
            completeness=100.0,
            provider_healthy="true",
            timestamp_valid="true",
            agreement=1.0,
            # sequence_ok omitted
        )
        resp_explicit = await _get(
            app,
            "/v1/quality/score",
            freshness="FRESH",
            completeness=100.0,
            provider_healthy="true",
            timestamp_valid="true",
            agreement=1.0,
            sequence_ok="true",
        )
        assert resp_default.json()["data"]["score"] == resp_explicit.json()["data"]["score"]

    @pytest.mark.asyncio
    async def test_grade_high_for_max_inputs(self) -> None:
        app = _make_app()
        resp = await _get(
            app,
            "/v1/quality/score",
            freshness="FRESH",
            completeness=100.0,
            provider_healthy="true",
            timestamp_valid="true",
            agreement=1.0,
        )
        assert resp.json()["data"]["grade"] == "HIGH"

    @pytest.mark.asyncio
    async def test_grade_blocked_for_low_score(self) -> None:
        """EXPIRED + 0% completeness + no provider + ... → BLOCKED."""
        app = _make_app()
        resp = await _get(
            app,
            "/v1/quality/score",
            freshness="EXPIRED",
            completeness=0.0,
            provider_healthy="false",
            timestamp_valid="false",
            agreement=0.0,
        )
        assert resp.json()["data"]["grade"] == "BLOCKED"

    @pytest.mark.asyncio
    async def test_signal_engine_allowed_true_when_score_gte_30(self) -> None:
        app = _make_app()
        resp = await _get(
            app,
            "/v1/quality/score",
            freshness="FRESH",
            completeness=100.0,
            provider_healthy="true",
            timestamp_valid="true",
            agreement=1.0,
        )
        data = resp.json()["data"]
        assert data["signalEngineAllowed"] is True

    @pytest.mark.asyncio
    async def test_signal_engine_blocked_when_score_lt_30(self) -> None:
        """Requirement 7.11: score < 30 → signalEngineAllowed always False."""
        app = _make_app()
        resp = await _get(
            app,
            "/v1/quality/score",
            freshness="EXPIRED",
            completeness=0.0,
            provider_healthy="false",
            timestamp_valid="false",
            agreement=0.0,
        )
        data = resp.json()["data"]
        assert data["signalEngineAllowed"] is False

    @pytest.mark.asyncio
    async def test_components_breakdown_present(self) -> None:
        app = _make_app()
        resp = await _get(
            app,
            "/v1/quality/score",
            freshness="FRESH",
            completeness=80.0,
            provider_healthy="true",
            timestamp_valid="false",
            agreement=0.5,
        )
        comps = resp.json()["data"]["components"]
        assert "freshness" in comps
        assert "freshnessScore" in comps
        assert "completenessScore" in comps
        assert "providerHealthScore" in comps
        assert "timestampScore" in comps
        assert "agreementScore" in comps
        assert "sequencePenaltyApplied" in comps

    @pytest.mark.asyncio
    async def test_component_values_match_formula(self) -> None:
        """Verify each component score matches the formula spec."""
        app = _make_app()
        resp = await _get(
            app,
            "/v1/quality/score",
            freshness="AGING",         # → 25
            completeness=80.0,          # → int(80 * 0.25) = 20
            provider_healthy="true",    # → 20
            timestamp_valid="false",    # → 0
            agreement=0.7,              # → int(0.7 * 10) = 7
            sequence_ok="true",         # → no penalty
        )
        comps = resp.json()["data"]["components"]
        assert comps["freshnessScore"] == 25
        assert comps["completenessScore"] == 20
        assert comps["providerHealthScore"] == 20
        assert comps["timestampScore"] == 0
        assert comps["agreementScore"] == 7
        assert comps["sequencePenaltyApplied"] is False

    @pytest.mark.asyncio
    async def test_freshness_case_insensitive(self) -> None:
        """freshness param should be normalised to uppercase."""
        app = _make_app()
        resp_lower = await _get(
            app,
            "/v1/quality/score",
            freshness="fresh",
            completeness=100.0,
            provider_healthy="true",
            timestamp_valid="true",
            agreement=1.0,
        )
        resp_upper = await _get(
            app,
            "/v1/quality/score",
            freshness="FRESH",
            completeness=100.0,
            provider_healthy="true",
            timestamp_valid="true",
            agreement=1.0,
        )
        assert resp_lower.status_code == 200
        assert resp_lower.json()["data"]["score"] == resp_upper.json()["data"]["score"]

    @pytest.mark.asyncio
    async def test_all_freshness_values_accepted(self) -> None:
        """All valid freshness strings must return 200."""
        app = _make_app()
        for freshness in ("FRESH", "AGING", "STALE", "EXPIRED", "UNKNOWN"):
            resp = await _get(
                app,
                "/v1/quality/score",
                freshness=freshness,
                completeness=50.0,
                provider_healthy="true",
                timestamp_valid="true",
                agreement=0.5,
            )
            assert resp.status_code == 200, f"Expected 200 for freshness={freshness}"


# ---------------------------------------------------------------------------
# GET /v1/quality/score — validation error cases
# ---------------------------------------------------------------------------


class TestGetQualityScoreValidation:
    """Tests for invalid query parameters to GET /v1/quality/score."""

    @pytest.mark.asyncio
    async def test_invalid_freshness_returns_400(self) -> None:
        app = _make_app()
        resp = await _get(
            app,
            "/v1/quality/score",
            freshness="SUPER_FRESH",
            completeness=100.0,
            provider_healthy="true",
            timestamp_valid="true",
            agreement=1.0,
        )
        assert resp.status_code == 400
        body = resp.json()
        assert "error" in body
        assert body["error"]["code"] == "INVALID_PARAMETER"

    @pytest.mark.asyncio
    async def test_completeness_above_100_returns_422(self) -> None:
        """FastAPI validates completeness <= 100 at the query level."""
        app = _make_app()
        resp = await _get(
            app,
            "/v1/quality/score",
            freshness="FRESH",
            completeness=150.0,
            provider_healthy="true",
            timestamp_valid="true",
            agreement=1.0,
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_completeness_below_0_returns_422(self) -> None:
        app = _make_app()
        resp = await _get(
            app,
            "/v1/quality/score",
            freshness="FRESH",
            completeness=-10.0,
            provider_healthy="true",
            timestamp_valid="true",
            agreement=1.0,
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_agreement_above_1_returns_422(self) -> None:
        app = _make_app()
        resp = await _get(
            app,
            "/v1/quality/score",
            freshness="FRESH",
            completeness=100.0,
            provider_healthy="true",
            timestamp_valid="true",
            agreement=1.5,
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_missing_required_params_returns_422(self) -> None:
        """freshness, completeness, provider_healthy, timestamp_valid, agreement
        are all required query parameters."""
        app = _make_app()
        resp = await _get(app, "/v1/quality/score")
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_error_envelope_has_code_message_requestid(self) -> None:
        app = _make_app()
        resp = await _get(
            app,
            "/v1/quality/score",
            freshness="BAD_VALUE",
            completeness=100.0,
            provider_healthy="true",
            timestamp_valid="true",
            agreement=1.0,
        )
        assert resp.status_code == 400
        err = resp.json()["error"]
        assert "code" in err
        assert "message" in err
        assert "requestId" in err
