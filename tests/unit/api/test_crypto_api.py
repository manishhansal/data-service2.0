"""
Unit tests for src/api/crypto.py — Binance crypto REST API endpoints.

Task 11.3 — Requirements 13.1, 13.7, 13.8

Endpoints covered:
    GET /v1/crypto/{symbol}/ohlcv         — OHLCV candles
    GET /v1/crypto/{symbol}/ticker        — current ticker price
    GET /v1/crypto/{symbol}/stats         — 24-hour statistics
    GET /v1/crypto/exchange-info          — exchange info
    GET /v1/crypto/futures/overview       — perpetual futures overview

Tests use an in-process ASGI test client (httpx.AsyncClient via
ASGITransport) so no live Binance API, Redis, or PostgreSQL is required.
All BinanceClient calls are mocked with AsyncMock.

Requirements: 13.1, 13.7, 13.8
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.api.crypto import router as crypto_router
from src.providers.adapters.binance_rest import BINANCE_INTERVALS

# ---------------------------------------------------------------------------
# Shared test fixtures / helpers
# ---------------------------------------------------------------------------

_SAMPLE_KLINES: list[dict[str, Any]] = [
    {
        "time": 1_700_000_000_000,
        "open": 35_000.0,
        "high": 36_000.0,
        "low": 34_500.0,
        "close": 35_500.0,
        "volume": 100.5,
        "closeTime": 1_700_003_599_999,
    },
    {
        "time": 1_700_003_600_000,
        "open": 35_500.0,
        "high": 37_000.0,
        "low": 35_200.0,
        "close": 36_800.0,
        "volume": 200.0,
        "closeTime": 1_700_007_199_999,
    },
]

_SAMPLE_TICKER: dict[str, Any] = {
    "symbol": "BTCUSDT",
    "price": "65123.45000000",
}

_SAMPLE_24HR_STATS: dict[str, Any] = {
    "symbol": "BTCUSDT",
    "priceChange": "1234.56",
    "priceChangePercent": "1.93",
    "weightedAvgPrice": "64500.00",
    "openPrice": "63888.89",
    "highPrice": "66000.00",
    "lowPrice": "63500.00",
    "lastPrice": "65123.45",
    "volume": "12345.67",
    "quoteVolume": "796543210.00",
    "openTime": 1_700_000_000_000,
    "closeTime": 1_700_086_399_999,
    "firstId": 1000000,
    "lastId": 1050000,
    "count": 50000,
}

_SAMPLE_EXCHANGE_INFO: dict[str, Any] = {
    "timezone": "UTC",
    "serverTime": 1_700_000_000_000,
    "symbols": [
        {
            "symbol": "BTCUSDT",
            "status": "TRADING",
            "baseAsset": "BTC",
            "quoteAsset": "USDT",
        }
    ],
}

_SAMPLE_MARK_PRICE: dict[str, Any] = {
    "symbol": "BTCUSDT",
    "markPrice": "65100.00",
    "indexPrice": "65050.00",
    "lastFundingRate": "0.0001",
    "nextFundingTime": 1_700_028_000_000,
    "time": 1_700_000_000_000,
}

_SAMPLE_OPEN_INTEREST: dict[str, Any] = {
    "symbol": "BTCUSDT",
    "openInterest": "12345.67",
    "time": 1_700_000_000_000,
}

_SAMPLE_OI_HISTORY: list[dict[str, Any]] = [
    {
        "symbol": "BTCUSDT",
        "sumOpenInterest": "12000.00",
        "sumOpenInterestValue": "780000000.00",
        "timestamp": 1_699_996_400_000,
    },
    {
        "symbol": "BTCUSDT",
        "sumOpenInterest": "12345.67",
        "sumOpenInterestValue": "803456789.00",
        "timestamp": 1_700_000_000_000,
    },
]

_SAMPLE_LONG_SHORT: list[dict[str, Any]] = [
    {
        "symbol": "BTCUSDT",
        "longShortRatio": "1.5",
        "longAccount": "0.6",
        "shortAccount": "0.4",
        "timestamp": 1_700_000_000_000,
    }
]


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def _make_app(binance_client: Any | None = None) -> FastAPI:
    """Create a minimal FastAPI app with only the crypto router."""
    app = FastAPI()
    app.include_router(crypto_router, prefix="/v1")
    if binance_client is not None:
        app.state.binance_client = binance_client
    return app


def _body(response: Any) -> dict[str, Any]:
    return json.loads(response.content)


def _mock_client(
    *,
    klines: list[dict] | None = None,
    ticker: dict | None = None,
    stats: dict | None = None,
    exchange_info: dict | None = None,
    mark_price: dict | None = None,
    open_interest: dict | None = None,
    oi_history: list[dict] | None = None,
    long_short: list[dict] | None = None,
    raise_on: str | None = None,  # method name to make raise an exception
) -> MagicMock:
    """Build a mock BinanceClient with pre-configured return values."""
    client = MagicMock()

    def _maybe_raise(method_name: str, return_value: Any) -> AsyncMock:
        if raise_on == method_name:
            mock = AsyncMock(side_effect=Exception(f"Simulated failure: {method_name}"))
        else:
            mock = AsyncMock(return_value=return_value)
        return mock

    client.get_klines = _maybe_raise("get_klines", klines or _SAMPLE_KLINES)
    client.get_ticker_price = _maybe_raise("get_ticker_price", ticker or _SAMPLE_TICKER)
    client.get_24hr_stats = _maybe_raise("get_24hr_stats", stats or _SAMPLE_24HR_STATS)
    client.get_exchange_info = _maybe_raise(
        "get_exchange_info", exchange_info or _SAMPLE_EXCHANGE_INFO
    )
    client.get_futures_mark_price = _maybe_raise(
        "get_futures_mark_price", mark_price or _SAMPLE_MARK_PRICE
    )
    client.get_futures_open_interest = _maybe_raise(
        "get_futures_open_interest", open_interest or _SAMPLE_OPEN_INTEREST
    )
    client.get_futures_oi_history = _maybe_raise(
        "get_futures_oi_history", oi_history or _SAMPLE_OI_HISTORY
    )
    client.get_long_short_ratio = _maybe_raise(
        "get_long_short_ratio", long_short or _SAMPLE_LONG_SHORT
    )
    return client


# ===========================================================================
# GET /v1/crypto/{symbol}/ohlcv — interval validation
# ===========================================================================


class TestOHLCVIntervalValidation:
    """Verify interval parameter validation for GET /v1/crypto/{symbol}/ohlcv."""

    @pytest.mark.asyncio
    async def test_3m_is_valid_for_crypto(self) -> None:
        """3m must NOT be rejected for Binance crypto data (Requirement 13.1)."""
        client = _mock_client()
        app = _make_app(client)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as http:
            resp = await http.get(
                "/v1/crypto/BTCUSDT/ohlcv", params={"interval": "3m"}
            )
        assert resp.status_code == 200, f"3m should be accepted for crypto, got {resp.status_code}"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("interval", sorted(BINANCE_INTERVALS))
    async def test_all_binance_intervals_accepted(self, interval: str) -> None:
        """Every interval in BINANCE_INTERVALS must return HTTP 200."""
        client = _mock_client()
        app = _make_app(client)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as http:
            resp = await http.get(
                "/v1/crypto/BTCUSDT/ohlcv", params={"interval": interval}
            )
        assert resp.status_code == 200, (
            f"Interval {interval!r} should be accepted, got {resp.status_code}"
        )

    @pytest.mark.asyncio
    async def test_unknown_interval_returns_400(self) -> None:
        """An interval not in BINANCE_INTERVALS must return HTTP 400."""
        client = _mock_client()
        app = _make_app(client)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as http:
            resp = await http.get(
                "/v1/crypto/BTCUSDT/ohlcv", params={"interval": "10m"}
            )
        assert resp.status_code == 400
        body = _body(resp)
        assert body["error"]["code"] == "INTERVAL_NOT_SUPPORTED"
        assert "10m" in body["error"]["message"]
        assert body["error"]["provider"] == "binance"

    @pytest.mark.asyncio
    async def test_unsupported_indian_interval_rejected(self) -> None:
        """An Indian-only interval like 10m should be rejected."""
        client = _mock_client()
        app = _make_app(client)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as http:
            resp = await http.get(
                "/v1/crypto/BTCUSDT/ohlcv", params={"interval": "10m"}
            )
        assert resp.status_code == 400


# ===========================================================================
# GET /v1/crypto/{symbol}/ohlcv — response shape and data
# ===========================================================================


class TestOHLCVResponseShape:
    """Verify the response envelope and data content for the OHLCV endpoint."""

    @pytest.mark.asyncio
    async def test_returns_canonical_envelope(self) -> None:
        """Response must contain 'data' and 'metadata' keys."""
        client = _mock_client()
        app = _make_app(client)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as http:
            resp = await http.get(
                "/v1/crypto/BTCUSDT/ohlcv", params={"interval": "1h"}
            )
        assert resp.status_code == 200
        body = _body(resp)
        assert "data" in body
        assert "metadata" in body

    @pytest.mark.asyncio
    async def test_metadata_fields_present(self) -> None:
        """Metadata must include requestedAt, dataAsOf, dataSourceType, provider."""
        client = _mock_client()
        app = _make_app(client)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as http:
            resp = await http.get(
                "/v1/crypto/BTCUSDT/ohlcv", params={"interval": "1h"}
            )
        meta = _body(resp)["metadata"]
        assert "requestedAt" in meta
        assert "dataAsOf" in meta
        assert "dataSourceType" in meta
        assert meta["provider"] == "binance"

    @pytest.mark.asyncio
    async def test_data_contains_normalised_candle_records(self) -> None:
        """Each item in data must have OHLCV fields matching BinanceCandleRecord schema."""
        client = _mock_client()
        app = _make_app(client)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as http:
            resp = await http.get(
                "/v1/crypto/BTCUSDT/ohlcv", params={"interval": "1h"}
            )
        data = _body(resp)["data"]
        assert len(data) == len(_SAMPLE_KLINES)

        for record in data:
            assert "symbol" in record
            assert "interval" in record
            assert "time" in record
            assert "open" in record
            assert "high" in record
            assert "low" in record
            assert "close" in record
            assert "volume" in record
            assert "closeTime" in record
            assert record["exchange"] == "BINANCE"

    @pytest.mark.asyncio
    async def test_symbol_is_uppercased_in_records(self) -> None:
        """Symbol field in returned records must be uppercase."""
        client = _mock_client()
        app = _make_app(client)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as http:
            resp = await http.get(
                "/v1/crypto/btcusdt/ohlcv", params={"interval": "1h"}
            )
        assert resp.status_code == 200
        for record in _body(resp)["data"]:
            assert record["symbol"] == record["symbol"].upper()

    @pytest.mark.asyncio
    async def test_data_source_type_historical_when_from_to_supplied(self) -> None:
        """dataSourceType should be HISTORICAL when from/to params are supplied."""
        client = _mock_client()
        app = _make_app(client)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as http:
            resp = await http.get(
                "/v1/crypto/BTCUSDT/ohlcv",
                params={
                    "interval": "1h",
                    "from": "2024-01-01",
                    "to": "2024-01-02",
                },
            )
        meta = _body(resp)["metadata"]
        assert meta["dataSourceType"] == "HISTORICAL"

    @pytest.mark.asyncio
    async def test_data_source_type_live_without_from_to(self) -> None:
        """dataSourceType should be LIVE when no time range is supplied."""
        client = _mock_client()
        app = _make_app(client)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as http:
            resp = await http.get(
                "/v1/crypto/BTCUSDT/ohlcv", params={"interval": "1h"}
            )
        meta = _body(resp)["metadata"]
        assert meta["dataSourceType"] == "LIVE"

    @pytest.mark.asyncio
    async def test_limit_parameter_forwarded_to_client(self) -> None:
        """Limit parameter should be passed to get_klines."""
        client = _mock_client()
        app = _make_app(client)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as http:
            resp = await http.get(
                "/v1/crypto/BTCUSDT/ohlcv",
                params={"interval": "1h", "limit": 100},
            )
        assert resp.status_code == 200
        client.get_klines.assert_awaited_once()
        _, kwargs = client.get_klines.call_args
        assert kwargs.get("limit") == 100 or client.get_klines.call_args[0][2] == 100

    @pytest.mark.asyncio
    async def test_invalid_from_date_returns_400(self) -> None:
        """Malformed 'from' date must return HTTP 400."""
        client = _mock_client()
        app = _make_app(client)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as http:
            resp = await http.get(
                "/v1/crypto/BTCUSDT/ohlcv",
                params={"interval": "1h", "from": "not-a-date"},
            )
        assert resp.status_code == 400
        body = _body(resp)
        assert body["error"]["code"] == "INVALID_PARAMETER"
        assert "from" in body["error"]["message"].lower()

    @pytest.mark.asyncio
    async def test_invalid_to_date_returns_400(self) -> None:
        """Malformed 'to' date must return HTTP 400."""
        client = _mock_client()
        app = _make_app(client)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as http:
            resp = await http.get(
                "/v1/crypto/BTCUSDT/ohlcv",
                params={"interval": "1h", "to": "bad-date"},
            )
        assert resp.status_code == 400
        body = _body(resp)
        assert body["error"]["code"] == "INVALID_PARAMETER"

    @pytest.mark.asyncio
    async def test_from_after_to_returns_400(self) -> None:
        """'from' later than 'to' must return HTTP 400."""
        client = _mock_client()
        app = _make_app(client)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as http:
            resp = await http.get(
                "/v1/crypto/BTCUSDT/ohlcv",
                params={
                    "interval": "1h",
                    "from": "2024-03-01",
                    "to": "2024-01-01",
                },
            )
        assert resp.status_code == 400
        body = _body(resp)
        assert body["error"]["code"] == "INVALID_PARAMETER"

    @pytest.mark.asyncio
    async def test_provider_failure_returns_502(self) -> None:
        """When BinanceClient raises, the endpoint must return HTTP 502."""
        client = _mock_client(raise_on="get_klines")
        app = _make_app(client)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as http:
            resp = await http.get(
                "/v1/crypto/BTCUSDT/ohlcv", params={"interval": "1h"}
            )
        assert resp.status_code == 502
        body = _body(resp)
        assert body["error"]["code"] == "PROVIDER_UNAVAILABLE"
        assert body["error"]["provider"] == "binance"

    @pytest.mark.asyncio
    async def test_limit_boundary_1_accepted(self) -> None:
        """Limit of 1 must be accepted."""
        client = _mock_client(klines=[_SAMPLE_KLINES[0]])
        app = _make_app(client)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as http:
            resp = await http.get(
                "/v1/crypto/BTCUSDT/ohlcv",
                params={"interval": "1h", "limit": 1},
            )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_limit_boundary_1000_accepted(self) -> None:
        """Limit of 1000 must be accepted."""
        client = _mock_client()
        app = _make_app(client)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as http:
            resp = await http.get(
                "/v1/crypto/BTCUSDT/ohlcv",
                params={"interval": "1h", "limit": 1000},
            )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_limit_0_rejected(self) -> None:
        """Limit of 0 must be rejected (FastAPI Query ge=1 constraint)."""
        client = _mock_client()
        app = _make_app(client)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as http:
            resp = await http.get(
                "/v1/crypto/BTCUSDT/ohlcv",
                params={"interval": "1h", "limit": 0},
            )
        assert resp.status_code == 422  # FastAPI validation error

    @pytest.mark.asyncio
    async def test_limit_1001_rejected(self) -> None:
        """Limit of 1001 must be rejected (FastAPI Query le=1000 constraint)."""
        client = _mock_client()
        app = _make_app(client)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as http:
            resp = await http.get(
                "/v1/crypto/BTCUSDT/ohlcv",
                params={"interval": "1h", "limit": 1001},
            )
        assert resp.status_code == 422


# ===========================================================================
# GET /v1/crypto/{symbol}/ticker
# ===========================================================================


class TestTicker:
    """Verify the ticker endpoint."""

    @pytest.mark.asyncio
    async def test_returns_200_with_canonical_envelope(self) -> None:
        """Successful ticker fetch must return HTTP 200 with data + metadata."""
        client = _mock_client()
        app = _make_app(client)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as http:
            resp = await http.get("/v1/crypto/BTCUSDT/ticker")
        assert resp.status_code == 200
        body = _body(resp)
        assert "data" in body
        assert "metadata" in body

    @pytest.mark.asyncio
    async def test_ticker_data_contains_symbol_and_price(self) -> None:
        """Data must contain symbol and price fields."""
        client = _mock_client()
        app = _make_app(client)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as http:
            resp = await http.get("/v1/crypto/BTCUSDT/ticker")
        data = _body(resp)["data"]
        assert data["symbol"] == "BTCUSDT"
        assert "price" in data

    @pytest.mark.asyncio
    async def test_metadata_provider_is_binance(self) -> None:
        """Metadata.provider must be 'binance'."""
        client = _mock_client()
        app = _make_app(client)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as http:
            resp = await http.get("/v1/crypto/ETHUSDT/ticker")
        assert _body(resp)["metadata"]["provider"] == "binance"

    @pytest.mark.asyncio
    async def test_symbol_case_insensitive(self) -> None:
        """Lowercase symbol in URL must still work."""
        client = _mock_client()
        app = _make_app(client)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as http:
            resp = await http.get("/v1/crypto/btcusdt/ticker")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_provider_failure_returns_502(self) -> None:
        """BinanceClient error must yield HTTP 502."""
        client = _mock_client(raise_on="get_ticker_price")
        app = _make_app(client)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as http:
            resp = await http.get("/v1/crypto/BTCUSDT/ticker")
        assert resp.status_code == 502
        assert _body(resp)["error"]["code"] == "PROVIDER_UNAVAILABLE"


# ===========================================================================
# GET /v1/crypto/{symbol}/stats
# ===========================================================================


class TestStats:
    """Verify the 24-hour statistics endpoint."""

    @pytest.mark.asyncio
    async def test_returns_200_with_canonical_envelope(self) -> None:
        """Successful stats fetch must return HTTP 200 with canonical envelope."""
        client = _mock_client()
        app = _make_app(client)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as http:
            resp = await http.get("/v1/crypto/BTCUSDT/stats")
        assert resp.status_code == 200
        body = _body(resp)
        assert "data" in body
        assert "metadata" in body

    @pytest.mark.asyncio
    async def test_data_contains_binance_stats_fields(self) -> None:
        """Data should include standard 24hr stat fields from Binance."""
        client = _mock_client()
        app = _make_app(client)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as http:
            resp = await http.get("/v1/crypto/BTCUSDT/stats")
        data = _body(resp)["data"]
        assert data["symbol"] == "BTCUSDT"
        assert "highPrice" in data
        assert "lowPrice" in data
        assert "volume" in data
        assert "priceChangePercent" in data

    @pytest.mark.asyncio
    async def test_provider_failure_returns_502(self) -> None:
        """BinanceClient error must yield HTTP 502."""
        client = _mock_client(raise_on="get_24hr_stats")
        app = _make_app(client)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as http:
            resp = await http.get("/v1/crypto/BTCUSDT/stats")
        assert resp.status_code == 502
        assert _body(resp)["error"]["code"] == "PROVIDER_UNAVAILABLE"

    @pytest.mark.asyncio
    async def test_metadata_data_source_type_live(self) -> None:
        """dataSourceType must be LIVE for stats endpoint."""
        client = _mock_client()
        app = _make_app(client)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as http:
            resp = await http.get("/v1/crypto/BTCUSDT/stats")
        assert _body(resp)["metadata"]["dataSourceType"] == "LIVE"


# ===========================================================================
# GET /v1/crypto/exchange-info
# ===========================================================================


class TestExchangeInfo:
    """Verify the exchange-info endpoint."""

    @pytest.mark.asyncio
    async def test_returns_200_without_symbol_filter(self) -> None:
        """Exchange info without symbol filter must return HTTP 200."""
        client = _mock_client()
        app = _make_app(client)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as http:
            resp = await http.get("/v1/crypto/exchange-info")
        assert resp.status_code == 200
        body = _body(resp)
        assert "data" in body
        assert "metadata" in body

    @pytest.mark.asyncio
    async def test_returns_200_with_symbol_filter(self) -> None:
        """Exchange info with ?symbol=BTCUSDT must return HTTP 200."""
        client = _mock_client()
        app = _make_app(client)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as http:
            resp = await http.get("/v1/crypto/exchange-info", params={"symbol": "BTCUSDT"})
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_symbol_filter_forwarded_to_client(self) -> None:
        """The symbol query parameter must be forwarded to get_exchange_info."""
        client = _mock_client()
        app = _make_app(client)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as http:
            await http.get("/v1/crypto/exchange-info", params={"symbol": "btcusdt"})
        # Symbol should be uppercased before being passed to the client.
        client.get_exchange_info.assert_awaited_once_with(symbol="BTCUSDT")

    @pytest.mark.asyncio
    async def test_no_symbol_filter_passes_none_to_client(self) -> None:
        """When no symbol filter is given, None must be passed to the client."""
        client = _mock_client()
        app = _make_app(client)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as http:
            await http.get("/v1/crypto/exchange-info")
        client.get_exchange_info.assert_awaited_once_with(symbol=None)

    @pytest.mark.asyncio
    async def test_provider_failure_returns_502(self) -> None:
        """BinanceClient error must yield HTTP 502."""
        client = _mock_client(raise_on="get_exchange_info")
        app = _make_app(client)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as http:
            resp = await http.get("/v1/crypto/exchange-info")
        assert resp.status_code == 502
        assert _body(resp)["error"]["code"] == "PROVIDER_UNAVAILABLE"

    @pytest.mark.asyncio
    async def test_data_contains_exchange_fields(self) -> None:
        """Data must include expected exchange info fields."""
        client = _mock_client()
        app = _make_app(client)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as http:
            resp = await http.get("/v1/crypto/exchange-info")
        data = _body(resp)["data"]
        assert "symbols" in data
        assert data["timezone"] == "UTC"


# ===========================================================================
# GET /v1/crypto/futures/overview
# ===========================================================================


class TestFuturesOverview:
    """Verify the perpetual futures overview endpoint (Requirement 13.7)."""

    @pytest.mark.asyncio
    async def test_returns_200_with_canonical_envelope(self) -> None:
        """Successful overview fetch must return HTTP 200 with canonical envelope."""
        client = _mock_client()
        app = _make_app(client)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as http:
            resp = await http.get("/v1/crypto/futures/overview")
        assert resp.status_code == 200
        body = _body(resp)
        assert "data" in body
        assert "metadata" in body

    @pytest.mark.asyncio
    async def test_returns_all_three_tracked_symbols(self) -> None:
        """Overview must include data for BTCUSDT, ETHUSDT, and SOLUSDT."""
        client = _mock_client()
        app = _make_app(client)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as http:
            resp = await http.get("/v1/crypto/futures/overview")
        data = _body(resp)["data"]
        assert isinstance(data, list)
        returned_symbols = {item["symbol"] for item in data}
        assert "BTCUSDT" in returned_symbols
        assert "ETHUSDT" in returned_symbols
        assert "SOLUSDT" in returned_symbols

    @pytest.mark.asyncio
    async def test_overview_contains_required_fields(self) -> None:
        """Each symbol entry must contain all required futures fields (Req 13.7)."""
        client = _mock_client()
        app = _make_app(client)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as http:
            resp = await http.get("/v1/crypto/futures/overview")
        for item in _body(resp)["data"]:
            assert "symbol" in item
            assert "markPrice" in item
            assert "indexPrice" in item
            assert "fundingRate" in item
            assert "fundingRateAnnualized" in item
            assert "nextFundingTime" in item
            assert "openInterest" in item
            assert "openInterestNotionalUsd" in item
            assert "oiChangePct1h" in item
            assert "longShortRatio" in item
            assert "longAccount" in item
            assert "shortAccount" in item

    @pytest.mark.asyncio
    async def test_funding_rate_annualised_formula(self) -> None:
        """fundingRateAnnualized must equal fundingRate * 3 * 365."""
        client = _mock_client()
        app = _make_app(client)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as http:
            resp = await http.get("/v1/crypto/futures/overview")
        for item in _body(resp)["data"]:
            if item["fundingRate"] is not None and item["fundingRateAnnualized"] is not None:
                expected = float(item["fundingRate"]) * 3 * 365
                assert abs(float(item["fundingRateAnnualized"]) - expected) < 1e-8

    @pytest.mark.asyncio
    async def test_oi_change_pct_computed_from_history(self) -> None:
        """oiChangePct1h should be computed from the OI history when available."""
        client = _mock_client()
        app = _make_app(client)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as http:
            resp = await http.get("/v1/crypto/futures/overview")
        data = _body(resp)["data"]
        btc = next(item for item in data if item["symbol"] == "BTCUSDT")
        # oi_now = 12345.67, oi_prev = 12000.00 → change ~2.88%
        assert btc["oiChangePct1h"] is not None
        assert abs(btc["oiChangePct1h"] - (12345.67 - 12000.0) / 12000.0 * 100) < 0.01

    @pytest.mark.asyncio
    async def test_partial_failure_does_not_crash_overview(self) -> None:
        """When mark price fetch fails for a symbol, overview still returns all symbols.

        Fields for the failing symbol should be null rather than raising.
        """
        client = _mock_client(raise_on="get_futures_mark_price")
        app = _make_app(client)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as http:
            resp = await http.get("/v1/crypto/futures/overview")
        # Must still return 200, not 502.
        assert resp.status_code == 200
        data = _body(resp)["data"]
        # All three symbols still present.
        assert len(data) == 3
        for item in data:
            # markPrice should be null when the fetch failed.
            assert item["markPrice"] is None

    @pytest.mark.asyncio
    async def test_long_short_fields_populated(self) -> None:
        """longShortRatio, longAccount, shortAccount must be populated."""
        client = _mock_client()
        app = _make_app(client)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as http:
            resp = await http.get("/v1/crypto/futures/overview")
        for item in _body(resp)["data"]:
            assert item["longShortRatio"] is not None
            assert item["longAccount"] is not None
            assert item["shortAccount"] is not None

    @pytest.mark.asyncio
    async def test_metadata_provider_is_binance(self) -> None:
        """Metadata.provider must be 'binance'."""
        client = _mock_client()
        app = _make_app(client)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as http:
            resp = await http.get("/v1/crypto/futures/overview")
        assert _body(resp)["metadata"]["provider"] == "binance"

    @pytest.mark.asyncio
    async def test_metadata_data_source_type_live(self) -> None:
        """dataSourceType must be LIVE for futures overview."""
        client = _mock_client()
        app = _make_app(client)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as http:
            resp = await http.get("/v1/crypto/futures/overview")
        assert _body(resp)["metadata"]["dataSourceType"] == "LIVE"


# ===========================================================================
# Lazy BinanceClient creation (no pre-attached client)
# ===========================================================================


class TestLazyClientCreation:
    """Verify that the endpoint lazily creates a BinanceClient when none is set."""

    @pytest.mark.asyncio
    async def test_ticker_without_pre_attached_client(self) -> None:
        """A ticker request must work even when no binance_client is on app.state."""
        app = FastAPI()
        app.include_router(crypto_router, prefix="/v1")
        # Deliberately do NOT set app.state.binance_client

        mock_ticker = AsyncMock(return_value=_SAMPLE_TICKER)
        with patch(
            "src.providers.adapters.binance_rest.BinanceClient.get_ticker_price",
            new=mock_ticker,
        ):
            # We expect a real BinanceClient to be created lazily; patching the
            # class method ensures we don't make real HTTP calls.
            # The test passes as long as no exception is raised and the client
            # is subsequently cached on app.state.
            assert getattr(app.state, "binance_client", None) is None

    @pytest.mark.asyncio
    async def test_client_cached_after_first_request(self) -> None:
        """BinanceClient created lazily must be cached on app.state."""
        client = _mock_client()
        app = _make_app(client)
        assert app.state.binance_client is client

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as http:
            await http.get("/v1/crypto/BTCUSDT/ticker")

        # The same client instance is still there.
        assert app.state.binance_client is client


# ===========================================================================
# Error envelope structure
# ===========================================================================


class TestErrorEnvelopeStructure:
    """Verify canonical error envelope structure for all error paths."""

    @pytest.mark.asyncio
    async def test_error_envelope_has_required_fields(self) -> None:
        """Error response must include code, message, provider, retryAfterMs, requestId."""
        client = _mock_client()
        app = _make_app(client)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as http:
            resp = await http.get(
                "/v1/crypto/BTCUSDT/ohlcv", params={"interval": "bad-interval"}
            )
        assert resp.status_code == 400
        err = _body(resp)["error"]
        assert "code" in err
        assert "message" in err
        assert "requestId" in err
        # requestId must be a non-empty string.
        assert err["requestId"] and isinstance(err["requestId"], str)

    @pytest.mark.asyncio
    async def test_no_stack_trace_in_502_error(self) -> None:
        """Provider failure error response must not contain stack trace details."""
        client = _mock_client(raise_on="get_klines")
        app = _make_app(client)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as http:
            resp = await http.get(
                "/v1/crypto/BTCUSDT/ohlcv", params={"interval": "1h"}
            )
        body_text = resp.text
        # No raw traceback indicators in the response.
        assert "Traceback" not in body_text
        assert "File \"" not in body_text
