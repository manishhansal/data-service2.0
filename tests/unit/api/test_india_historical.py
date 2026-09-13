"""
Unit tests for the historical API endpoints added in Task 7.4.

Endpoints covered:
    GET  /v1/india/historical                    — OHLCV data query
    GET  /v1/india/historical/status             — coverage + gap summary
    POST /v1/india/historical/backfill           — trigger async backfill
    GET  /v1/india/historical/backfill/{job_id}  — poll backfill status

Tests use an in-process ASGI test client (httpx.AsyncClient via
ASGITransport) so no live Redis or PostgreSQL is required.

Requirements: 4.1, 4.2, 4.9, 4.10, 10.1, 10.2, 10.8, 10.9, 16.10
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.api.india import router as india_router, _backfill_jobs

# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def _make_app(*, db_engine=None, redis=None) -> FastAPI:
    """Create a minimal FastAPI app with just the india router."""
    app = FastAPI()
    app.include_router(india_router, prefix="/v1")

    # Attach state attributes used by the endpoints.
    app.state.db_engine = db_engine
    app.state.redis = redis
    # Do NOT attach historical_engine or gap_recovery_engine — let endpoints
    # lazily create them so we test the lazy-init path too.
    return app


_SIMPLE_APP = _make_app()


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _body(response) -> dict[str, Any]:
    return json.loads(response.content)


# ===========================================================================
# GET /v1/india/historical — interval validation
# ===========================================================================


class TestGetHistoricalIntervalValidation:
    """Verify interval parameter validation for GET /v1/india/historical."""

    @pytest.mark.asyncio
    async def test_3m_returns_400(self):
        """3m must be rejected with INTERVAL_NOT_SUPPORTED (Req 4.2, 16.10)."""
        async with AsyncClient(
            transport=ASGITransport(app=_SIMPLE_APP), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/v1/india/historical",
                params={"symbol": "NIFTY", "interval": "3m"},
            )
        assert resp.status_code == 400
        body = _body(resp)
        assert body["error"]["code"] == "INTERVAL_NOT_SUPPORTED"
        assert "3m" in body["error"]["message"]
        assert "permanently unsupported" in body["error"]["message"]

    @pytest.mark.asyncio
    async def test_unsupported_interval_returns_400(self):
        """An unrecognised interval that is not 3m must also return 400."""
        async with AsyncClient(
            transport=ASGITransport(app=_SIMPLE_APP), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/v1/india/historical",
                params={"symbol": "RELIANCE", "interval": "2h"},
            )
        assert resp.status_code == 400
        body = _body(resp)
        assert body["error"]["code"] == "INTERVAL_NOT_SUPPORTED"
        assert "2h" in body["error"]["message"]

    @pytest.mark.asyncio
    async def test_valid_interval_1m_returns_200(self):
        """1m is a valid Indian-market interval."""
        async with AsyncClient(
            transport=ASGITransport(app=_SIMPLE_APP), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/v1/india/historical",
                params={
                    "symbol": "RELIANCE",
                    "interval": "1m",
                    "from": "2024-01-01",
                    "to": "2024-01-02",
                },
            )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "interval",
        ["1m", "5m", "10m", "15m", "30m", "1h", "1d", "1w", "1M"],
    )
    async def test_all_canonical_intervals_accepted(self, interval: str):
        """Every canonical Indian-market timeframe returns 200."""
        async with AsyncClient(
            transport=ASGITransport(app=_SIMPLE_APP), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/v1/india/historical",
                params={
                    "symbol": "NIFTY",
                    "interval": interval,
                    "from": "2024-01-01",
                    "to": "2024-01-02",
                },
            )
        assert resp.status_code == 200, f"Expected 200 for interval={interval}, got {resp.status_code}"


# ===========================================================================
# GET /v1/india/historical — date parameter validation
# ===========================================================================


class TestGetHistoricalDateValidation:
    """Verify from/to date parsing and range validation."""

    @pytest.mark.asyncio
    async def test_malformed_from_date_returns_400(self):
        async with AsyncClient(
            transport=ASGITransport(app=_SIMPLE_APP), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/v1/india/historical",
                params={"symbol": "RELIANCE", "interval": "1d", "from": "not-a-date"},
            )
        assert resp.status_code == 400
        body = _body(resp)
        assert body["error"]["code"] == "INVALID_PARAMETER"
        assert "from" in body["error"]["message"].lower()

    @pytest.mark.asyncio
    async def test_malformed_to_date_returns_400(self):
        async with AsyncClient(
            transport=ASGITransport(app=_SIMPLE_APP), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/v1/india/historical",
                params={
                    "symbol": "RELIANCE",
                    "interval": "1d",
                    "from": "2024-01-01",
                    "to": "bad-date",
                },
            )
        assert resp.status_code == 400
        body = _body(resp)
        assert body["error"]["code"] == "INVALID_PARAMETER"
        assert "to" in body["error"]["message"].lower()

    @pytest.mark.asyncio
    async def test_from_after_to_returns_400(self):
        """from >= to must be rejected."""
        async with AsyncClient(
            transport=ASGITransport(app=_SIMPLE_APP), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/v1/india/historical",
                params={
                    "symbol": "RELIANCE",
                    "interval": "1d",
                    "from": "2024-03-01",
                    "to": "2024-01-01",
                },
            )
        assert resp.status_code == 400
        body = _body(resp)
        assert body["error"]["code"] == "INVALID_PARAMETER"

    @pytest.mark.asyncio
    async def test_from_equals_to_returns_400(self):
        """from == to is an invalid range."""
        async with AsyncClient(
            transport=ASGITransport(app=_SIMPLE_APP), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/v1/india/historical",
                params={
                    "symbol": "RELIANCE",
                    "interval": "1d",
                    "from": "2024-01-01",
                    "to": "2024-01-01",
                },
            )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_iso_datetime_with_z_suffix_accepted(self):
        """Full ISO-8601 datetime with Z suffix is accepted."""
        async with AsyncClient(
            transport=ASGITransport(app=_SIMPLE_APP), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/v1/india/historical",
                params={
                    "symbol": "NIFTY",
                    "interval": "1h",
                    "from": "2024-01-01T09:15:00Z",
                    "to": "2024-01-01T15:30:00Z",
                },
            )
        assert resp.status_code == 200


# ===========================================================================
# GET /v1/india/historical — response envelope
# ===========================================================================


class TestGetHistoricalResponseEnvelope:
    """Verify the canonical response envelope shape."""

    @pytest.mark.asyncio
    async def test_response_has_data_and_metadata(self):
        """Response must include top-level 'data' and 'metadata' keys."""
        async with AsyncClient(
            transport=ASGITransport(app=_SIMPLE_APP), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/v1/india/historical",
                params={
                    "symbol": "RELIANCE",
                    "interval": "1d",
                    "from": "2024-01-01",
                    "to": "2024-01-10",
                },
            )
        assert resp.status_code == 200
        body = _body(resp)
        assert "data" in body
        assert "metadata" in body

    @pytest.mark.asyncio
    async def test_metadata_contains_required_fields(self):
        """metadata must include requestedAt, dataSourceType, truncated, gaps."""
        async with AsyncClient(
            transport=ASGITransport(app=_SIMPLE_APP), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/v1/india/historical",
                params={
                    "symbol": "NIFTY",
                    "interval": "1d",
                    "from": "2024-01-01",
                    "to": "2024-01-10",
                },
            )
        body = _body(resp)
        meta = body["metadata"]
        assert "requestedAt" in meta
        assert meta["dataSourceType"] == "HISTORICAL"
        assert "truncated" in meta
        assert "gaps" in meta
        assert isinstance(meta["gaps"], list)

    @pytest.mark.asyncio
    async def test_data_is_list(self):
        """The 'data' field must be a list (even when empty)."""
        async with AsyncClient(
            transport=ASGITransport(app=_SIMPLE_APP), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/v1/india/historical",
                params={
                    "symbol": "RELIANCE",
                    "interval": "1d",
                    "from": "2024-01-01",
                    "to": "2024-01-10",
                },
            )
        body = _body(resp)
        assert isinstance(body["data"], list)

    @pytest.mark.asyncio
    async def test_no_db_returns_empty_list(self):
        """When db_engine is None, data should be an empty list (no crash)."""
        app = _make_app(db_engine=None)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/v1/india/historical",
                params={
                    "symbol": "RELIANCE",
                    "interval": "1d",
                    "from": "2024-01-01",
                    "to": "2024-01-10",
                },
            )
        assert resp.status_code == 200
        assert _body(resp)["data"] == []

    @pytest.mark.asyncio
    async def test_requestedAt_is_utc_iso8601(self):
        """requestedAt must be a UTC ISO-8601 string ending in Z."""
        async with AsyncClient(
            transport=ASGITransport(app=_SIMPLE_APP), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/v1/india/historical",
                params={
                    "symbol": "NIFTY",
                    "interval": "1d",
                    "from": "2024-01-01",
                    "to": "2024-01-10",
                },
            )
        meta = _body(resp)["metadata"]
        assert meta["requestedAt"].endswith("Z"), (
            f"requestedAt should end with 'Z', got {meta['requestedAt']!r}"
        )


# ===========================================================================
# GET /v1/india/historical/status
# ===========================================================================


class TestGetHistoricalStatus:
    """Verify GET /v1/india/historical/status response shape."""

    @pytest.mark.asyncio
    async def test_returns_200(self):
        async with AsyncClient(
            transport=ASGITransport(app=_SIMPLE_APP), base_url="http://test"
        ) as client:
            resp = await client.get("/v1/india/historical/status")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_response_contains_required_top_level_keys(self):
        """Response data must include all required top-level fields."""
        async with AsyncClient(
            transport=ASGITransport(app=_SIMPLE_APP), base_url="http://test"
        ) as client:
            resp = await client.get("/v1/india/historical/status")
        data = _body(resp)["data"]
        assert "supportedTimeframes" in data
        assert "gapSummary" in data
        assert "reconciliationStatus" in data
        assert "backfillJobs" in data
        assert "providerActivity" in data

    @pytest.mark.asyncio
    async def test_supported_timeframes_excludes_3m(self):
        """supportedTimeframes must not include the permanently-banned 3m."""
        async with AsyncClient(
            transport=ASGITransport(app=_SIMPLE_APP), base_url="http://test"
        ) as client:
            resp = await client.get("/v1/india/historical/status")
        timeframes = _body(resp)["data"]["supportedTimeframes"]
        assert "3m" not in timeframes, "3m must never appear in supportedTimeframes"

    @pytest.mark.asyncio
    async def test_supported_timeframes_contains_canonical_intervals(self):
        """All canonical Indian-market intervals must be present."""
        expected = {"1m", "5m", "10m", "15m", "30m", "1h", "1d", "1w", "1M"}
        async with AsyncClient(
            transport=ASGITransport(app=_SIMPLE_APP), base_url="http://test"
        ) as client:
            resp = await client.get("/v1/india/historical/status")
        timeframes = set(_body(resp)["data"]["supportedTimeframes"])
        assert expected.issubset(timeframes), (
            f"Missing intervals: {expected - timeframes}"
        )

    @pytest.mark.asyncio
    async def test_gap_summary_has_by_status_breakdown(self):
        """gapSummary must have total and byStatus sub-fields."""
        async with AsyncClient(
            transport=ASGITransport(app=_SIMPLE_APP), base_url="http://test"
        ) as client:
            resp = await client.get("/v1/india/historical/status")
        gap_summary = _body(resp)["data"]["gapSummary"]
        assert "total" in gap_summary
        assert "byStatus" in gap_summary
        by_status = gap_summary["byStatus"]
        for key in ("PENDING", "RECOVERING", "RECOVERED", "EXHAUSTED"):
            assert key in by_status

    @pytest.mark.asyncio
    async def test_reconciliation_status_fields(self):
        """reconciliationStatus must contain the three required fields."""
        async with AsyncClient(
            transport=ASGITransport(app=_SIMPLE_APP), base_url="http://test"
        ) as client:
            resp = await client.get("/v1/india/historical/status")
        recon = _body(resp)["data"]["reconciliationStatus"]
        assert "totalCompared" in recon
        assert "matched" in recon
        assert "matchRatePct" in recon

    @pytest.mark.asyncio
    async def test_data_source_type_is_derived(self):
        """Status endpoint serves derived (not live) data."""
        async with AsyncClient(
            transport=ASGITransport(app=_SIMPLE_APP), base_url="http://test"
        ) as client:
            resp = await client.get("/v1/india/historical/status")
        assert _body(resp)["metadata"]["dataSourceType"] == "DERIVED"


# ===========================================================================
# POST /v1/india/historical/backfill
# ===========================================================================


class TestPostBackfill:
    """Verify POST /v1/india/historical/backfill."""

    @pytest.mark.asyncio
    async def test_valid_request_returns_202(self):
        """A well-formed request should return HTTP 202 Accepted."""
        async with AsyncClient(
            transport=ASGITransport(app=_SIMPLE_APP), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/v1/india/historical/backfill",
                json={
                    "symbol": "RELIANCE",
                    "exchange": "NSE",
                    "interval": "1d",
                    "from_date": "2024-01-01",
                    "to_date": "2024-03-01",
                    "instrument_class": "EQ",
                },
            )
        assert resp.status_code == 202

    @pytest.mark.asyncio
    async def test_response_contains_job_id(self):
        """Response data must include jobId."""
        async with AsyncClient(
            transport=ASGITransport(app=_SIMPLE_APP), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/v1/india/historical/backfill",
                json={
                    "symbol": "NIFTY",
                    "exchange": "NSE",
                    "interval": "1h",
                    "from_date": "2024-01-01",
                    "to_date": "2024-01-31",
                },
            )
        data = _body(resp)["data"]
        assert "jobId" in data
        # jobId should look like a UUID.
        assert re.match(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
            data["jobId"],
        ), f"jobId is not a UUID: {data['jobId']!r}"

    @pytest.mark.asyncio
    async def test_initial_status_is_pending_or_running(self):
        """The job should start in PENDING or RUNNING state immediately."""
        async with AsyncClient(
            transport=ASGITransport(app=_SIMPLE_APP), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/v1/india/historical/backfill",
                json={
                    "symbol": "BANKNIFTY",
                    "interval": "1d",
                    "from_date": "2024-01-01",
                    "to_date": "2024-01-31",
                },
            )
        data = _body(resp)["data"]
        assert data["status"] in ("PENDING", "RUNNING")

    @pytest.mark.asyncio
    async def test_3m_interval_returns_422(self):
        """3m interval in request body must be rejected (Pydantic validation)."""
        async with AsyncClient(
            transport=ASGITransport(app=_SIMPLE_APP), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/v1/india/historical/backfill",
                json={
                    "symbol": "RELIANCE",
                    "interval": "3m",
                    "from_date": "2024-01-01",
                    "to_date": "2024-01-31",
                },
            )
        # Pydantic v2 returns 422 for validation failures.
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_unsupported_interval_returns_422(self):
        """An unrecognised interval in the body must be rejected."""
        async with AsyncClient(
            transport=ASGITransport(app=_SIMPLE_APP), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/v1/india/historical/backfill",
                json={
                    "symbol": "RELIANCE",
                    "interval": "4h",
                    "from_date": "2024-01-01",
                    "to_date": "2024-01-31",
                },
            )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_malformed_from_date_returns_400(self):
        """Malformed from_date should return 400."""
        async with AsyncClient(
            transport=ASGITransport(app=_SIMPLE_APP), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/v1/india/historical/backfill",
                json={
                    "symbol": "RELIANCE",
                    "interval": "1d",
                    "from_date": "not-a-date",
                    "to_date": "2024-01-31",
                },
            )
        assert resp.status_code == 400
        assert _body(resp)["error"]["code"] == "INVALID_PARAMETER"

    @pytest.mark.asyncio
    async def test_from_after_to_returns_400(self):
        """from_date > to_date must return 400."""
        async with AsyncClient(
            transport=ASGITransport(app=_SIMPLE_APP), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/v1/india/historical/backfill",
                json={
                    "symbol": "RELIANCE",
                    "interval": "1d",
                    "from_date": "2024-03-01",
                    "to_date": "2024-01-01",
                },
            )
        assert resp.status_code == 400
        assert _body(resp)["error"]["code"] == "INVALID_PARAMETER"

    @pytest.mark.asyncio
    async def test_invalid_instrument_class_returns_422(self):
        """An unrecognised instrument_class must be rejected by Pydantic."""
        async with AsyncClient(
            transport=ASGITransport(app=_SIMPLE_APP), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/v1/india/historical/backfill",
                json={
                    "symbol": "NIFTY",
                    "interval": "1d",
                    "from_date": "2024-01-01",
                    "instrument_class": "CRYPTO",
                },
            )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_to_date_defaults_to_now(self):
        """to_date may be omitted; the endpoint defaults it to now."""
        async with AsyncClient(
            transport=ASGITransport(app=_SIMPLE_APP), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/v1/india/historical/backfill",
                json={
                    "symbol": "RELIANCE",
                    "interval": "1d",
                    "from_date": "2024-01-01",
                },
            )
        assert resp.status_code == 202


# ===========================================================================
# GET /v1/india/historical/backfill/{job_id}
# ===========================================================================


class TestGetBackfillStatus:
    """Verify GET /v1/india/historical/backfill/{job_id}."""

    @pytest.mark.asyncio
    async def test_unknown_job_id_returns_404(self):
        """A job_id that was never created must return HTTP 404."""
        async with AsyncClient(
            transport=ASGITransport(app=_SIMPLE_APP), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/v1/india/historical/backfill/00000000-0000-0000-0000-000000000000"
            )
        assert resp.status_code == 404
        body = _body(resp)
        assert body["error"]["code"] == "NOT_FOUND"

    @pytest.mark.asyncio
    async def test_known_job_id_returns_200_with_record(self):
        """A job created via POST should be retrievable via GET."""
        async with AsyncClient(
            transport=ASGITransport(app=_SIMPLE_APP), base_url="http://test"
        ) as client:
            post_resp = await client.post(
                "/v1/india/historical/backfill",
                json={
                    "symbol": "SBIN",
                    "interval": "1d",
                    "from_date": "2024-01-01",
                    "to_date": "2024-06-01",
                },
            )
            assert post_resp.status_code == 202
            job_id = _body(post_resp)["data"]["jobId"]

            get_resp = await client.get(
                f"/v1/india/historical/backfill/{job_id}"
            )

        assert get_resp.status_code == 200
        data = _body(get_resp)["data"]
        assert data["jobId"] == job_id
        assert data["symbol"] == "SBIN"

    @pytest.mark.asyncio
    async def test_job_record_contains_required_fields(self):
        """The job record must include all expected tracking fields."""
        async with AsyncClient(
            transport=ASGITransport(app=_SIMPLE_APP), base_url="http://test"
        ) as client:
            post_resp = await client.post(
                "/v1/india/historical/backfill",
                json={
                    "symbol": "TCS",
                    "interval": "1h",
                    "from_date": "2024-01-01",
                    "to_date": "2024-02-01",
                },
            )
            job_id = _body(post_resp)["data"]["jobId"]
            resp = await client.get(f"/v1/india/historical/backfill/{job_id}")

        data = _body(resp)["data"]
        required = {
            "jobId", "symbol", "exchange", "interval",
            "fromDate", "toDate", "status", "createdAt",
        }
        missing = required - set(data.keys())
        assert not missing, f"Missing fields in job record: {missing}"

    @pytest.mark.asyncio
    async def test_status_field_is_valid(self):
        """The status field must be one of the known values."""
        valid_statuses = {"PENDING", "RUNNING", "COMPLETED", "FAILED"}
        async with AsyncClient(
            transport=ASGITransport(app=_SIMPLE_APP), base_url="http://test"
        ) as client:
            post_resp = await client.post(
                "/v1/india/historical/backfill",
                json={
                    "symbol": "HDFC",
                    "interval": "5m",
                    "from_date": "2024-01-01",
                    "to_date": "2024-01-10",
                },
            )
            job_id = _body(post_resp)["data"]["jobId"]
            resp = await client.get(f"/v1/india/historical/backfill/{job_id}")

        status = _body(resp)["data"]["status"]
        assert status in valid_statuses, (
            f"Unexpected status value: {status!r}. Must be one of {valid_statuses}"
        )

    @pytest.mark.asyncio
    async def test_response_uses_canonical_envelope(self):
        """GET backfill/{job_id} must return the canonical data + metadata envelope."""
        async with AsyncClient(
            transport=ASGITransport(app=_SIMPLE_APP), base_url="http://test"
        ) as client:
            post_resp = await client.post(
                "/v1/india/historical/backfill",
                json={
                    "symbol": "WIPRO",
                    "interval": "1d",
                    "from_date": "2024-01-01",
                    "to_date": "2024-03-01",
                },
            )
            job_id = _body(post_resp)["data"]["jobId"]
            resp = await client.get(f"/v1/india/historical/backfill/{job_id}")

        body = _body(resp)
        assert "data" in body
        assert "metadata" in body
