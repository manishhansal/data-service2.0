"""
Unit tests for Delta Exchange endpoints in src/api/crypto.py

Tests:
- GET /v1/delta/{symbol}/ohlcv — happy path, bad interval, bad date params
- GET /v1/delta/{symbol}/ticker — happy path, provider error
- GET /v1/delta/futures/overview — happy path, provider errors gracefully handled
- 3m interval accepted for Delta (crypto exception)

Requirements: DS2-RCA-001, 13.1
"""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("CORS_ALLOWED_ORIGINS", "http://localhost:3000")
os.environ.setdefault("ENVIRONMENT", "development")
os.environ.setdefault("CONSUMER_API_KEYS", "test-key-delta")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-must-be-32-chars!!")

from src.api.crypto import router  # noqa: E402

HEADERS = {"X-API-Key": "test-key-delta"}


def _canonical_candle(symbol: str = "BTCUSD", interval: str = "1h") -> dict[str, Any]:
    return {
        "time":      1_700_000_000_000,
        "open":      43_000.0,
        "high":      43_500.0,
        "low":       42_800.0,
        "close":     43_200.0,
        "volume":    15.5,
        "closeTime": 1_700_003_599_999,
        "symbol":    symbol,
        "interval":  interval,
        "exchange":  "DELTA",
    }


def _normalised_ticker(symbol: str = "BTCUSD") -> dict[str, Any]:
    return {
        "symbol":       symbol,
        "price":        43_200.0,
        "change":       200.0,
        "changePct":    0.465,
        "high":         43_500.0,
        "low":          42_800.0,
        "volume":       1_500.0,
        "quoteVolume":  64_800_000.0,
        "markPrice":    43_210.0,
        "indexPrice":   43_190.0,
        "openInterest": 5_000.0,
        "ts":           1_700_000_000_000,
        "exchange":     "DELTA",
    }


def _make_app_with_delta_client(mock_client: Any) -> FastAPI:
    app = FastAPI()
    app.include_router(router, prefix="/v1")
    app.state.delta_client = mock_client
    app.state.binance_client = MagicMock()
    app.state.redis = None
    app.state.db_engine = None
    return app


def _make_delta_client(
    *,
    candles=None,
    ticker=None,
    tickers=None,
    premium=None,
    oi_history=None,
) -> MagicMock:
    client = MagicMock()
    client.get_candles = AsyncMock(return_value=candles or [])
    client.get_ticker = AsyncMock(return_value=ticker or _normalised_ticker())
    client.get_tickers = AsyncMock(return_value=tickers or [_normalised_ticker()])
    client.get_premium_index = AsyncMock(return_value=premium or {
        "symbol": "BTCUSD",
        "markPrice": 43_210.0,
        "indexPrice": 43_190.0,
        "fundingRate": 0.0001,
        "fundingRateAnnualized": 0.1095,
        "nextFundingTime": 0,
        "ts": 1_700_000_000_000,
        "exchange": "DELTA",
    })
    client.get_oi_history = AsyncMock(return_value=oi_history or [
        {"ts": 1_700_000_000_000, "openInterest": 4_900.0, "notionalUsd": 0},
        {"ts": 1_700_003_600_000, "openInterest": 5_000.0, "notionalUsd": 0},
    ])
    return client


# ---------------------------------------------------------------------------
# GET /v1/delta/{symbol}/ohlcv
# ---------------------------------------------------------------------------

class TestDeltaOHLCV:
    @pytest.mark.asyncio
    async def test_returns_200_with_candles(self) -> None:
        raw_candles = [_canonical_candle()]
        mock_client = _make_delta_client(candles=raw_candles)
        app = _make_app_with_delta_client(mock_client)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/v1/delta/BTCUSD/ohlcv?interval=1h&limit=10", headers=HEADERS)
        assert r.status_code == 200
        body = r.json()
        assert body["metadata"]["provider"] == "delta"
        assert isinstance(body["data"], list)

    @pytest.mark.asyncio
    async def test_3m_interval_accepted(self) -> None:
        """3m is valid for Delta crypto — must NOT return 400."""
        mock_client = _make_delta_client(candles=[_canonical_candle(interval="3m")])
        app = _make_app_with_delta_client(mock_client)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/v1/delta/BTCUSD/ohlcv?interval=3m&limit=5", headers=HEADERS)
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_unsupported_interval_returns_400(self) -> None:
        """10m is not a Delta interval — must return 400."""
        mock_client = _make_delta_client()
        app = _make_app_with_delta_client(mock_client)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/v1/delta/BTCUSD/ohlcv?interval=10m&limit=5", headers=HEADERS)
        assert r.status_code == 400
        body = r.json()
        assert body["error"]["code"] == "INTERVAL_NOT_SUPPORTED"

    @pytest.mark.asyncio
    async def test_invalid_from_date_returns_400(self) -> None:
        mock_client = _make_delta_client()
        app = _make_app_with_delta_client(mock_client)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/v1/delta/BTCUSD/ohlcv?interval=1h&from=not-a-date", headers=HEADERS)
        assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_provider_error_returns_502(self) -> None:
        mock_client = MagicMock()
        mock_client.get_candles = AsyncMock(side_effect=RuntimeError("Delta unavailable"))
        app = _make_app_with_delta_client(mock_client)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/v1/delta/BTCUSD/ohlcv?interval=1h", headers=HEADERS)
        assert r.status_code == 502

    @pytest.mark.asyncio
    async def test_from_before_to_validation(self) -> None:
        mock_client = _make_delta_client()
        app = _make_app_with_delta_client(mock_client)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(
                "/v1/delta/BTCUSD/ohlcv?interval=1h&from=2024-03-01&to=2024-01-01",
                headers=HEADERS,
            )
        assert r.status_code == 400


# ---------------------------------------------------------------------------
# GET /v1/delta/{symbol}/ticker
# ---------------------------------------------------------------------------

class TestDeltaTicker:
    @pytest.mark.asyncio
    async def test_returns_200_with_ticker(self) -> None:
        mock_client = _make_delta_client(ticker=_normalised_ticker("BTCUSD"))
        app = _make_app_with_delta_client(mock_client)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/v1/delta/BTCUSD/ticker", headers=HEADERS)
        assert r.status_code == 200
        body = r.json()
        assert body["data"]["symbol"] == "BTCUSD"
        assert body["metadata"]["provider"] == "delta"

    @pytest.mark.asyncio
    async def test_provider_error_returns_502(self) -> None:
        mock_client = MagicMock()
        mock_client.get_ticker = AsyncMock(side_effect=RuntimeError("network error"))
        app = _make_app_with_delta_client(mock_client)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/v1/delta/BTCUSD/ticker", headers=HEADERS)
        assert r.status_code == 502


# ---------------------------------------------------------------------------
# GET /v1/delta/futures/overview
# ---------------------------------------------------------------------------

class TestDeltaFuturesOverview:
    @pytest.mark.asyncio
    async def test_returns_200_with_overview(self) -> None:
        mock_client = _make_delta_client()
        app = _make_app_with_delta_client(mock_client)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/v1/delta/futures/overview", headers=HEADERS)
        assert r.status_code == 200
        body = r.json()
        assert isinstance(body["data"], list)
        assert len(body["data"]) == 3  # BTCUSD, ETHUSD, SOLUSD
        assert body["metadata"]["provider"] == "delta"

    @pytest.mark.asyncio
    async def test_tracked_symbols_in_response(self) -> None:
        mock_client = _make_delta_client()
        app = _make_app_with_delta_client(mock_client)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/v1/delta/futures/overview", headers=HEADERS)
        symbols = {item["symbol"] for item in r.json()["data"]}
        assert symbols == {"BTCUSD", "ETHUSD", "SOLUSD"}

    @pytest.mark.asyncio
    async def test_long_short_ratio_is_null(self) -> None:
        """Delta India has no L/S ratio — must be null in response."""
        mock_client = _make_delta_client()
        app = _make_app_with_delta_client(mock_client)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/v1/delta/futures/overview", headers=HEADERS)
        for item in r.json()["data"]:
            assert item["longShortRatio"] is None
            assert item["longAccount"] is None
            assert item["shortAccount"] is None

    @pytest.mark.asyncio
    async def test_provider_errors_handled_gracefully(self) -> None:
        """Individual symbol failures must not crash the whole overview."""
        mock_client = MagicMock()
        mock_client.get_premium_index = AsyncMock(side_effect=RuntimeError("rate limited"))
        mock_client.get_ticker = AsyncMock(side_effect=RuntimeError("timeout"))
        mock_client.get_oi_history = AsyncMock(side_effect=RuntimeError("unavailable"))
        app = _make_app_with_delta_client(mock_client)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/v1/delta/futures/overview", headers=HEADERS)
        # Must still return 200 — partial data is acceptable.
        assert r.status_code == 200
        assert len(r.json()["data"]) == 3
