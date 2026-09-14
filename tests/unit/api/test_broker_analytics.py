"""
Unit tests for src/api/broker_analytics.py

Tests:
- 503 when adapter not configured
- 200 with correct data when adapter is configured
- 502 when adapter raises an exception
- Canonical envelope shape on all responses

Requirements: DS2-RCA-018, Requirement 21
"""

from __future__ import annotations

import json
import os
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("CORS_ALLOWED_ORIGINS", "http://localhost:3000")
os.environ.setdefault("ENVIRONMENT", "development")
os.environ.setdefault("CONSUMER_API_KEYS", "test-key-analytics")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-must-be-32-chars!!")

from src.api.broker_analytics import router  # noqa: E402


def _make_app(angel_adapter: Any = None) -> FastAPI:
    """Build a minimal FastAPI app with the broker_analytics router."""
    app = FastAPI()
    app.include_router(router, prefix="/v1")
    app.state.angel_one_adapter = angel_adapter
    app.state.redis = None
    app.state.db_engine = None
    return app


def _make_adapter(**method_returns) -> MagicMock:
    """Build a mock AngelOneAdapter with async methods."""
    adapter = MagicMock()
    for method, return_value in method_returns.items():
        setattr(adapter, method, AsyncMock(return_value=return_value))
    return adapter


HEADERS = {"X-API-Key": "test-key-analytics"}


class TestBrokerAnalyticsPCR:
    @pytest.mark.asyncio
    async def test_503_when_no_adapter(self) -> None:
        app = _make_app(angel_adapter=None)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.get("/v1/india/broker-analytics/pcr", headers=HEADERS)
        assert r.status_code == 503
        body = r.json()
        assert body["error"]["code"] == "PROVIDER_NOT_CONFIGURED"

    @pytest.mark.asyncio
    async def test_200_with_pcr_data(self) -> None:
        pcr_data = {"putCallRatio": 1.23, "provider": "angel_one"}
        adapter = _make_adapter(fetch_pcr=pcr_data)
        app = _make_app(angel_adapter=adapter)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.get("/v1/india/broker-analytics/pcr", headers=HEADERS)
        assert r.status_code == 200
        body = r.json()
        assert body["data"] == pcr_data
        assert body["metadata"]["provider"] == "angel_one"

    @pytest.mark.asyncio
    async def test_502_when_adapter_raises(self) -> None:
        adapter = MagicMock()
        adapter.fetch_pcr = AsyncMock(side_effect=RuntimeError("SmartAPI unavailable"))
        app = _make_app(angel_adapter=adapter)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.get("/v1/india/broker-analytics/pcr", headers=HEADERS)
        assert r.status_code == 502
        body = r.json()
        assert body["error"]["code"] == "PROVIDER_UNAVAILABLE"


class TestBrokerAnalyticsOIBuildup:
    @pytest.mark.asyncio
    async def test_503_when_no_adapter(self) -> None:
        app = _make_app(angel_adapter=None)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.get("/v1/india/broker-analytics/oi-buildup", headers=HEADERS)
        assert r.status_code == 503
        assert r.json()["error"]["code"] == "PROVIDER_NOT_CONFIGURED"

    @pytest.mark.asyncio
    async def test_200_with_oi_buildup_list(self) -> None:
        records = [
            {"symbol": "RELIANCE", "oi": 12000, "buildupType": "LONG_BUILDUP"},
            {"symbol": "TCS", "oi": 8000, "buildupType": "SHORT_COVERING"},
        ]
        adapter = _make_adapter(fetch_oi_buildup=records)
        app = _make_app(angel_adapter=adapter)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.get("/v1/india/broker-analytics/oi-buildup", headers=HEADERS)
        assert r.status_code == 200
        body = r.json()
        assert len(body["data"]) == 2
        assert body["data"][0]["symbol"] == "RELIANCE"

    @pytest.mark.asyncio
    async def test_502_when_adapter_raises(self) -> None:
        adapter = MagicMock()
        adapter.fetch_oi_buildup = AsyncMock(side_effect=RuntimeError("timeout"))
        app = _make_app(angel_adapter=adapter)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.get("/v1/india/broker-analytics/oi-buildup", headers=HEADERS)
        assert r.status_code == 502


class TestBrokerAnalyticsGainersLosers:
    @pytest.mark.asyncio
    async def test_503_when_no_adapter(self) -> None:
        app = _make_app(angel_adapter=None)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.get("/v1/india/broker-analytics/gainers-losers", headers=HEADERS)
        assert r.status_code == 503

    @pytest.mark.asyncio
    async def test_200_with_gainers_losers_data(self) -> None:
        gl_data = {
            "oiGainers": [{"symbol": "NIFTY", "change": 5.2}],
            "oiLosers": [],
            "priceGainers": [],
            "priceLosers": [],
            "provider": "angel_one",
        }
        adapter = _make_adapter(fetch_gainers_losers=gl_data)
        app = _make_app(angel_adapter=adapter)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.get("/v1/india/broker-analytics/gainers-losers", headers=HEADERS)
        assert r.status_code == 200
        body = r.json()
        assert "oiGainers" in body["data"]
        assert body["metadata"]["dataSourceType"] == "LIVE"

    @pytest.mark.asyncio
    async def test_502_when_adapter_raises(self) -> None:
        adapter = MagicMock()
        adapter.fetch_gainers_losers = AsyncMock(side_effect=Exception("upstream error"))
        app = _make_app(angel_adapter=adapter)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.get("/v1/india/broker-analytics/gainers-losers", headers=HEADERS)
        assert r.status_code == 502


class TestEnvelopeShape:
    """Verify the canonical envelope shape is consistent across all endpoints."""

    @pytest.mark.asyncio
    async def test_success_envelope_has_required_fields(self) -> None:
        adapter = _make_adapter(fetch_pcr={"putCallRatio": 1.0})
        app = _make_app(angel_adapter=adapter)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.get("/v1/india/broker-analytics/pcr", headers=HEADERS)
        body = r.json()
        assert "data" in body
        assert "metadata" in body
        assert "requestedAt" in body["metadata"]
        assert "dataSourceType" in body["metadata"]
        assert "provider" in body["metadata"]

    @pytest.mark.asyncio
    async def test_error_envelope_has_required_fields(self) -> None:
        app = _make_app(angel_adapter=None)  # no adapter → 503
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.get("/v1/india/broker-analytics/pcr", headers=HEADERS)
        body = r.json()
        assert "error" in body
        err = body["error"]
        assert "code" in err
        assert "message" in err
        assert "requestId" in err
