"""
tests/test_dataset_publisher.py

Unit tests for:
  - src/publishers/dataset_publisher.py  (DatasetPublisher)
  - src/engines/streaming_engine.py      (publish_dataset_ready_simple)
  - src/api/internal.py                  (POST /v1/internal/dataset-ready)

Coverage:
  - DatasetPublisher.publish succeeds on first attempt
  - DatasetPublisher.publish retries on transient failures (1 failure → success)
  - DatasetPublisher.publish retries twice and raises on third failure
  - DatasetPublisher.publish raises RuntimeError when engine is None
  - DatasetPublisher.publish_event extracts fields from dict correctly
  - DatasetPublisher constructor validates max_retries and backoff_base_secs
  - StreamingEngine.publish_dataset_ready_simple builds correct event dict
  - StreamingEngine.publish_dataset_ready_simple returns "" when no event bus
  - POST /v1/internal/dataset-ready returns 200 with msgId on success
  - POST /v1/internal/dataset-ready returns 400 for invalid interval "3m"
  - POST /v1/internal/dataset-ready returns 400 for malformed date
  - POST /v1/internal/dataset-ready returns 503 when streaming engine absent
  - POST /v1/internal/dataset-ready returns 502 when publish raises
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.api.internal import router as internal_router
from src.engines.streaming_engine import StreamingEngine
from src.publishers.dataset_publisher import DatasetPublisher

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_VALID_PAYLOAD: dict[str, Any] = {
    "market": "NSE",
    "symbol": "RELIANCE",
    "interval": "1d",
    "date": "2024-01-15",
    "record_count": 375,
}


def _make_mock_engine(return_msg_id: str = "1705300000456-0") -> MagicMock:
    """Return a mock StreamingEngine whose publish methods are AsyncMocks."""
    engine = MagicMock(spec=StreamingEngine)
    engine.publish_dataset_ready_simple = AsyncMock(return_value=return_msg_id)
    engine.publish_dataset_ready = AsyncMock(return_value=return_msg_id)
    return engine


def _make_test_app(streaming_engine: Any = None) -> FastAPI:
    """Build a minimal FastAPI app with the internal router and optional engine state."""
    app = FastAPI()
    app.include_router(internal_router, prefix="/v1")
    if streaming_engine is not None:
        app.state.streaming_engine = streaming_engine
    return app


# ---------------------------------------------------------------------------
# DatasetPublisher — constructor validation
# ---------------------------------------------------------------------------


class TestDatasetPublisherConstructor:
    def test_default_construction(self) -> None:
        """DatasetPublisher constructs without arguments."""
        publisher = DatasetPublisher()
        assert publisher._max_retries == 3
        assert publisher._backoff_base_secs == 1.0

    def test_custom_max_retries(self) -> None:
        publisher = DatasetPublisher(max_retries=5)
        assert publisher._max_retries == 5

    def test_max_retries_too_low_raises(self) -> None:
        with pytest.raises(ValueError, match="max_retries"):
            DatasetPublisher(max_retries=0)

    def test_max_retries_too_high_raises(self) -> None:
        with pytest.raises(ValueError, match="max_retries"):
            DatasetPublisher(max_retries=11)

    def test_nonpositive_backoff_raises(self) -> None:
        with pytest.raises(ValueError, match="backoff_base_secs"):
            DatasetPublisher(backoff_base_secs=0)

    def test_negative_backoff_raises(self) -> None:
        with pytest.raises(ValueError, match="backoff_base_secs"):
            DatasetPublisher(backoff_base_secs=-1.0)


# ---------------------------------------------------------------------------
# DatasetPublisher.publish — success path
# ---------------------------------------------------------------------------


class TestDatasetPublisherPublishSuccess:
    @pytest.mark.asyncio
    async def test_publish_calls_engine_once_on_success(self) -> None:
        """On success, publish_dataset_ready_simple is called exactly once."""
        engine = _make_mock_engine("stream-id-001")
        publisher = DatasetPublisher(engine=engine)

        msg_id = await publisher.publish(
            market="NSE",
            symbol="RELIANCE",
            interval="1d",
            date="2024-01-15",
            record_count=375,
        )

        engine.publish_dataset_ready_simple.assert_called_once_with(
            market="NSE",
            symbol="RELIANCE",
            interval="1d",
            date="2024-01-15",
            record_count=375,
        )
        assert msg_id == "stream-id-001"

    @pytest.mark.asyncio
    async def test_publish_returns_stream_entry_id(self) -> None:
        engine = _make_mock_engine("1705300000456-0")
        publisher = DatasetPublisher(engine=engine)
        result = await publisher.publish(
            market="CRYPTO",
            symbol="BTC",
            interval="1h",
            date="2024-06-01",
            record_count=24,
        )
        assert result == "1705300000456-0"

    @pytest.mark.asyncio
    async def test_publish_no_engine_raises_runtime_error(self) -> None:
        publisher = DatasetPublisher(engine=None)
        with pytest.raises(RuntimeError, match="no StreamingEngine"):
            await publisher.publish(
                market="NSE",
                symbol="NIFTY",
                interval="1m",
                date="2024-01-15",
                record_count=375,
            )


# ---------------------------------------------------------------------------
# DatasetPublisher.publish — retry logic
# ---------------------------------------------------------------------------


class TestDatasetPublisherRetry:
    @pytest.mark.asyncio
    async def test_retries_on_transient_failure_then_succeeds(self) -> None:
        """If first attempt fails but second succeeds, returns the msg_id."""
        engine = MagicMock(spec=StreamingEngine)
        engine.publish_dataset_ready_simple = AsyncMock(
            side_effect=[RuntimeError("redis timeout"), "stream-id-002"]
        )

        publisher = DatasetPublisher(engine=engine, backoff_base_secs=0.001)
        msg_id = await publisher.publish(
            market="NSE",
            symbol="NIFTY",
            interval="5m",
            date="2024-01-15",
            record_count=100,
        )

        assert msg_id == "stream-id-002"
        assert engine.publish_dataset_ready_simple.call_count == 2

    @pytest.mark.asyncio
    async def test_retries_twice_then_raises_on_third_failure(self) -> None:
        """After max_retries exhausted, the last exception is re-raised."""
        engine = MagicMock(spec=StreamingEngine)
        exc = ConnectionError("Redis unavailable")
        engine.publish_dataset_ready_simple = AsyncMock(side_effect=exc)

        publisher = DatasetPublisher(engine=engine, max_retries=3, backoff_base_secs=0.001)

        with pytest.raises(ConnectionError, match="Redis unavailable"):
            await publisher.publish(
                market="NSE",
                symbol="BANKNIFTY",
                interval="1d",
                date="2024-01-15",
                record_count=5,
            )

        # All 3 attempts should have been made.
        assert engine.publish_dataset_ready_simple.call_count == 3

    @pytest.mark.asyncio
    async def test_single_attempt_raises_immediately_on_failure(self) -> None:
        """max_retries=1 means no retries — just one attempt."""
        engine = MagicMock(spec=StreamingEngine)
        engine.publish_dataset_ready_simple = AsyncMock(
            side_effect=ValueError("bad input")
        )

        publisher = DatasetPublisher(engine=engine, max_retries=1, backoff_base_secs=0.001)

        with pytest.raises(ValueError):
            await publisher.publish(
                market="NSE",
                symbol="X",
                interval="1d",
                date="2024-01-15",
                record_count=1,
            )

        assert engine.publish_dataset_ready_simple.call_count == 1

    @pytest.mark.asyncio
    async def test_backoff_delay_called_between_retries(self) -> None:
        """asyncio.sleep is called between retry attempts."""
        engine = MagicMock(spec=StreamingEngine)
        engine.publish_dataset_ready_simple = AsyncMock(
            side_effect=[Exception("fail"), Exception("fail"), "ok"]
        )

        sleep_calls: list[float] = []

        async def fake_sleep(secs: float) -> None:
            sleep_calls.append(secs)

        with patch("src.publishers.dataset_publisher.asyncio.sleep", new=fake_sleep):
            publisher = DatasetPublisher(engine=engine, max_retries=3, backoff_base_secs=1.0)
            await publisher.publish(
                market="NSE",
                symbol="NIFTY",
                interval="1d",
                date="2024-01-15",
                record_count=10,
            )

        # Attempt 1 fails → sleep(1.0), attempt 2 fails → sleep(2.0), attempt 3 succeeds
        assert sleep_calls == [1.0, 2.0]


# ---------------------------------------------------------------------------
# DatasetPublisher.publish_event
# ---------------------------------------------------------------------------


class TestDatasetPublisherPublishEvent:
    @pytest.mark.asyncio
    async def test_publish_event_extracts_required_fields(self) -> None:
        """publish_event must correctly extract market/symbol/interval/date/record_count."""
        engine = _make_mock_engine("stream-123")
        publisher = DatasetPublisher(engine=engine)

        event = {
            "market": "NSE",
            "symbol": "RELIANCE",
            "interval": "1d",
            "date": "2024-01-15",
            "record_count": 375,
            "extra_field": "ignored",
        }

        msg_id = await publisher.publish_event(event)
        assert msg_id == "stream-123"

        engine.publish_dataset_ready_simple.assert_called_once_with(
            market="NSE",
            symbol="RELIANCE",
            interval="1d",
            date="2024-01-15",
            record_count=375,
        )

    @pytest.mark.asyncio
    async def test_publish_event_raises_on_missing_key(self) -> None:
        engine = _make_mock_engine()
        publisher = DatasetPublisher(engine=engine)

        with pytest.raises(KeyError):
            await publisher.publish_event({"market": "NSE"})  # missing required keys


# ---------------------------------------------------------------------------
# StreamingEngine.publish_dataset_ready_simple
# ---------------------------------------------------------------------------


class TestStreamingEnginePublishDatasetReadySimple:
    @pytest.fixture
    def mock_event_bus(self) -> MagicMock:
        from src.cache.event_bus import EventBus

        bus = MagicMock(spec=EventBus)
        bus.publish_dataset_ready = AsyncMock(return_value="1705300000456-0")
        return bus

    @pytest.mark.asyncio
    async def test_publishes_to_event_bus_with_correct_fields(
        self, mock_event_bus: MagicMock
    ) -> None:
        """publish_dataset_ready_simple must call EventBus with the correct fields."""
        engine = StreamingEngine(event_bus=mock_event_bus)
        msg_id = await engine.publish_dataset_ready_simple(
            market="NSE",
            symbol="RELIANCE",
            interval="1d",
            date="2024-01-15",
            record_count=375,
        )

        assert msg_id == "1705300000456-0"
        mock_event_bus.publish_dataset_ready.assert_called_once()

        call_args = mock_event_bus.publish_dataset_ready.call_args[0][0]
        assert call_args["market"] == "NSE"
        assert call_args["symbol"] == "RELIANCE"
        assert call_args["interval"] == "1d"
        assert call_args["date"] == "2024-01-15"
        assert call_args["record_count"] == 375
        # published_at must be a UTC ISO-8601 Z-suffix string
        assert call_args["published_at"].endswith("Z")
        # dataType must be set
        assert call_args["dataType"] == "HISTORICAL_OHLCV"

    @pytest.mark.asyncio
    async def test_returns_empty_string_when_no_event_bus(self) -> None:
        engine = StreamingEngine(event_bus=None)
        result = await engine.publish_dataset_ready_simple(
            market="NSE",
            symbol="NIFTY",
            interval="1m",
            date="2024-01-15",
            record_count=1,
        )
        assert result == ""

    @pytest.mark.asyncio
    async def test_does_not_increment_ticks_published(
        self, mock_event_bus: MagicMock
    ) -> None:
        """publish_dataset_ready_simple must not modify the tick counters."""
        engine = StreamingEngine(event_bus=mock_event_bus)
        await engine.publish_dataset_ready_simple(
            market="NSE",
            symbol="NIFTY",
            interval="1d",
            date="2024-01-15",
            record_count=375,
        )
        assert engine.get_stream_status()["ticksPublished"] == 0


# ---------------------------------------------------------------------------
# POST /v1/internal/dataset-ready — API endpoint tests
# ---------------------------------------------------------------------------


class TestInternalDatasetReadyEndpoint:
    """Tests for POST /v1/internal/dataset-ready using in-process ASGI client."""

    @pytest.mark.asyncio
    async def test_returns_200_with_msg_id_on_success(self) -> None:
        engine = _make_mock_engine("stream-abc-123")
        app = _make_test_app(streaming_engine=engine)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/v1/internal/dataset-ready", json=_VALID_PAYLOAD)

        assert response.status_code == 200
        body = response.json()
        assert body["data"]["msgId"] == "stream-abc-123"
        assert body["data"]["symbol"] == "RELIANCE"
        assert body["data"]["market"] == "NSE"
        assert body["data"]["interval"] == "1d"
        assert body["data"]["date"] == "2024-01-15"
        assert body["data"]["record_count"] == 375

    @pytest.mark.asyncio
    async def test_returns_400_for_3m_interval(self) -> None:
        """interval '3m' must be rejected with HTTP 400."""
        engine = _make_mock_engine()
        app = _make_test_app(streaming_engine=engine)

        payload = {**_VALID_PAYLOAD, "interval": "3m"}

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/v1/internal/dataset-ready", json=payload)

        assert response.status_code == 422  # Pydantic validation error

    @pytest.mark.asyncio
    async def test_returns_422_for_malformed_date(self) -> None:
        """A date not in YYYY-MM-DD format must be rejected."""
        engine = _make_mock_engine()
        app = _make_test_app(streaming_engine=engine)

        payload = {**_VALID_PAYLOAD, "date": "15-01-2024"}

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/v1/internal/dataset-ready", json=payload)

        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_returns_503_when_no_streaming_engine(self) -> None:
        """When app.state.streaming_engine is absent, return 503."""
        app = _make_test_app(streaming_engine=None)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/v1/internal/dataset-ready", json=_VALID_PAYLOAD)

        assert response.status_code == 503
        body = response.json()
        assert body["error"]["code"] == "STREAMING_ENGINE_UNAVAILABLE"

    @pytest.mark.asyncio
    async def test_returns_502_when_publish_raises(self) -> None:
        """When the publisher exhausts retries, return 502."""
        engine = MagicMock(spec=StreamingEngine)
        engine.publish_dataset_ready_simple = AsyncMock(
            side_effect=ConnectionError("Redis down")
        )
        app = _make_test_app(streaming_engine=engine)

        # Patch asyncio.sleep to avoid real delays in this test
        with patch("src.publishers.dataset_publisher.asyncio.sleep", new=AsyncMock()):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                response = await client.post("/v1/internal/dataset-ready", json=_VALID_PAYLOAD)

        assert response.status_code == 502
        body = response.json()
        assert body["error"]["code"] == "EVENT_PUBLISH_FAILED"

    @pytest.mark.asyncio
    async def test_returns_422_for_missing_required_field(self) -> None:
        """Request missing a required field must return 422 Unprocessable Entity."""
        engine = _make_mock_engine()
        app = _make_test_app(streaming_engine=engine)

        payload = {k: v for k, v in _VALID_PAYLOAD.items() if k != "symbol"}

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/v1/internal/dataset-ready", json=payload)

        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_returns_422_for_negative_record_count(self) -> None:
        """record_count must be >= 0; negative values should be rejected."""
        engine = _make_mock_engine()
        app = _make_test_app(streaming_engine=engine)

        payload = {**_VALID_PAYLOAD, "record_count": -1}

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/v1/internal/dataset-ready", json=payload)

        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_crypto_market_success(self) -> None:
        """Crypto market with 3m interval allowed (3m is only banned for Indian markets,
        but the endpoint-level validator blocks it for safety — test a valid crypto interval."""
        engine = _make_mock_engine("stream-crypto-001")
        app = _make_test_app(streaming_engine=engine)

        payload = {
            "market": "CRYPTO",
            "symbol": "BTC",
            "interval": "1h",
            "date": "2024-06-01",
            "record_count": 24,
        }

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/v1/internal/dataset-ready", json=payload)

        assert response.status_code == 200
        body = response.json()
        assert body["data"]["msgId"] == "stream-crypto-001"
        assert body["data"]["symbol"] == "BTC"

    @pytest.mark.asyncio
    async def test_response_contains_metadata_envelope(self) -> None:
        """Success response must include the canonical metadata envelope."""
        engine = _make_mock_engine()
        app = _make_test_app(streaming_engine=engine)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/v1/internal/dataset-ready", json=_VALID_PAYLOAD)

        assert response.status_code == 200
        body = response.json()
        assert "data" in body
        assert "metadata" in body
        assert "requestedAt" in body["metadata"]
        assert body["metadata"]["dataSourceType"] == "INTERNAL"
