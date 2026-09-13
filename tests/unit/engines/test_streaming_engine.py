"""
tests/unit/engines/test_streaming_engine.py

Unit tests for src/engines/streaming_engine.py — StreamingEngine.

Covers (Task 8.5, Requirements 3.5, 3.6, 15.2, 8.7):
  - publish_tick sends to Event Bus
  - Duplicate tick sets isDuplicate=True and still publishes
  - Non-duplicate tick sets isDuplicate=False
  - get_stream_status returns correct structure
  - ticksPublished counter increments correctly on each publish_tick call
  - publish_dataset_ready sends to correct Event Bus stream
  - Broker connection state management
  - Validation failure recording
  - Symbol subscription / unsubscription tracking
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.cache.event_bus import EventBus
from src.core.validators.dedup import DedupStore
from src.engines.streaming_engine import StreamingEngine

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tick(
    instrument_id: str = "NSE:NIFTY:IDX",
    symbol: str = "NIFTY",
    event_time_ms: int = 1_705_300_000_000,
    ltp: float = 22_000.0,
    volume: int = 1_000_000,
    source: str = "angel_one",
) -> dict:
    """Return a minimal canonical tick dict."""
    return {
        "tickId": "tick-uuid-001",
        "instrumentId": instrument_id,
        "symbol": symbol,
        "exchange": "NSE",
        "eventTimeMs": event_time_ms,
        "receivedAtMs": event_time_ms + 50,
        "ltp": ltp,
        "change": 10.0,
        "changePct": 0.05,
        "volume": volume,
        "oi": None,
        "tradedValue": 500_000_000.0,
        "source": source,
        "quality": 0.92,
    }


def _make_dataset_ready_event() -> dict:
    return {
        "dataType": "HISTORICAL_OHLCV",
        "instrumentId": "NSE:RELIANCE:EQ",
        "exchange": "NSE",
        "intervalStr": "1m",
        "fromTs": 1_705_300_000_000,
        "toTs": 1_705_386_400_000,
        "rowCount": 375,
        "provider": "angel_one",
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_event_bus() -> MagicMock:
    """Return a MagicMock EventBus whose async methods are AsyncMocks."""
    bus = MagicMock(spec=EventBus)
    bus.publish_tick = AsyncMock(return_value="1705300000123-0")
    bus.publish_dataset_ready = AsyncMock(return_value="1705300000456-0")
    return bus


@pytest.fixture
def dedup_store() -> DedupStore:
    """Return a fresh DedupStore for each test."""
    store = DedupStore()
    store.clear()
    return store


@pytest.fixture
def engine(mock_event_bus: MagicMock, dedup_store: DedupStore) -> StreamingEngine:
    """Return a StreamingEngine with mocked EventBus and fresh DedupStore."""
    return StreamingEngine(event_bus=mock_event_bus, dedup_store=dedup_store)


# ---------------------------------------------------------------------------
# Tests — publish_tick
# ---------------------------------------------------------------------------


class TestPublishTick:
    """Tests for StreamingEngine.publish_tick."""

    @pytest.mark.asyncio
    async def test_publish_tick_calls_event_bus(
        self, engine: StreamingEngine, mock_event_bus: MagicMock
    ) -> None:
        """publish_tick must forward the tick to EventBus.publish_tick."""
        tick = _make_tick()
        msg_id = await engine.publish_tick(tick)

        mock_event_bus.publish_tick.assert_called_once()
        call_args = mock_event_bus.publish_tick.call_args
        # First positional arg is symbol, second is the tick dict.
        assert call_args[0][0] == "NIFTY"
        assert call_args[0][1] is tick
        assert msg_id == "1705300000123-0"

    @pytest.mark.asyncio
    async def test_unique_tick_sets_is_duplicate_false(
        self, engine: StreamingEngine
    ) -> None:
        """First time a tick's hash is seen → isDuplicate must be False."""
        tick = _make_tick()
        await engine.publish_tick(tick)
        assert tick["isDuplicate"] is False

    @pytest.mark.asyncio
    async def test_duplicate_tick_sets_is_duplicate_true(
        self, engine: StreamingEngine, mock_event_bus: MagicMock
    ) -> None:
        """Sending the identical tick twice → second publish has isDuplicate=True."""
        tick1 = _make_tick()
        tick2 = _make_tick()  # same field values → same hash

        await engine.publish_tick(tick1)
        await engine.publish_tick(tick2)

        assert tick1["isDuplicate"] is False
        assert tick2["isDuplicate"] is True

    @pytest.mark.asyncio
    async def test_duplicate_tick_still_published_to_event_bus(
        self, engine: StreamingEngine, mock_event_bus: MagicMock
    ) -> None:
        """Duplicate ticks must NOT be silently dropped — they must still be
        published to the Event Bus (Requirement 3.6 auditability rule)."""
        tick1 = _make_tick()
        tick2 = _make_tick()

        await engine.publish_tick(tick1)
        await engine.publish_tick(tick2)

        # Both publish calls must reach the Event Bus.
        assert mock_event_bus.publish_tick.call_count == 2

    @pytest.mark.asyncio
    async def test_ticks_with_different_ltp_are_not_duplicates(
        self, engine: StreamingEngine
    ) -> None:
        """Ticks differing in ltp should have different hashes → not duplicates."""
        tick_a = _make_tick(ltp=22_000.0)
        tick_b = _make_tick(ltp=22_001.0)

        await engine.publish_tick(tick_a)
        await engine.publish_tick(tick_b)

        assert tick_a["isDuplicate"] is False
        assert tick_b["isDuplicate"] is False

    @pytest.mark.asyncio
    async def test_ticks_with_different_event_time_are_not_duplicates(
        self, engine: StreamingEngine
    ) -> None:
        """Ticks differing only in eventTimeMs should not be flagged as duplicates."""
        tick_a = _make_tick(event_time_ms=1_705_300_000_000)
        tick_b = _make_tick(event_time_ms=1_705_300_001_000)

        await engine.publish_tick(tick_a)
        await engine.publish_tick(tick_b)

        assert tick_a["isDuplicate"] is False
        assert tick_b["isDuplicate"] is False

    @pytest.mark.asyncio
    async def test_returns_empty_string_when_no_event_bus(
        self, dedup_store: DedupStore
    ) -> None:
        """publish_tick with no Event Bus returns '' but does not raise."""
        engine = StreamingEngine(event_bus=None, dedup_store=dedup_store)
        tick = _make_tick()
        result = await engine.publish_tick(tick)
        assert result == ""

    @pytest.mark.asyncio
    async def test_publish_tick_returns_message_id(
        self, engine: StreamingEngine
    ) -> None:
        """publish_tick should return the stream entry ID from the Event Bus."""
        tick = _make_tick()
        msg_id = await engine.publish_tick(tick)
        assert msg_id == "1705300000123-0"


# ---------------------------------------------------------------------------
# Tests — ticksPublished counter
# ---------------------------------------------------------------------------


class TestTicksPublishedCounter:
    """Tests for the ticksPublished counter in get_stream_status."""

    @pytest.mark.asyncio
    async def test_counter_starts_at_zero(self, engine: StreamingEngine) -> None:
        status = engine.get_stream_status()
        assert status["ticksPublished"] == 0

    @pytest.mark.asyncio
    async def test_counter_increments_on_each_publish(
        self, engine: StreamingEngine
    ) -> None:
        """ticksPublished must increment for each publish_tick call, including
        duplicate ticks."""
        tick1 = _make_tick()
        tick2 = _make_tick()  # duplicate
        tick3 = _make_tick(ltp=99.9)  # unique

        await engine.publish_tick(tick1)
        assert engine.get_stream_status()["ticksPublished"] == 1

        await engine.publish_tick(tick2)
        assert engine.get_stream_status()["ticksPublished"] == 2

        await engine.publish_tick(tick3)
        assert engine.get_stream_status()["ticksPublished"] == 3

    @pytest.mark.asyncio
    async def test_duplicate_count_increments_for_duplicates_only(
        self, engine: StreamingEngine
    ) -> None:
        """duplicateCount must only increment when isDuplicate=True."""
        tick1 = _make_tick()
        tick2 = _make_tick()  # duplicate of tick1
        tick3 = _make_tick(ltp=999.0)  # unique

        await engine.publish_tick(tick1)
        await engine.publish_tick(tick2)
        await engine.publish_tick(tick3)

        status = engine.get_stream_status()
        assert status["duplicateCount"] == 1
        assert status["ticksPublished"] == 3


# ---------------------------------------------------------------------------
# Tests — get_stream_status structure
# ---------------------------------------------------------------------------


class TestGetStreamStatus:
    """Tests for get_stream_status return value shape and content."""

    def test_returns_all_required_keys(self, engine: StreamingEngine) -> None:
        status = engine.get_stream_status()
        required_keys = {
            "subscribedSymbols",
            "ticksPublished",
            "duplicateCount",
            "validationFailures",
            "lastPublishedAt",
            "brokerConnections",
        }
        assert required_keys.issubset(status.keys())

    def test_initial_state(self, engine: StreamingEngine) -> None:
        status = engine.get_stream_status()
        assert status["subscribedSymbols"] == []
        assert status["ticksPublished"] == 0
        assert status["duplicateCount"] == 0
        assert status["validationFailures"] == {}
        assert status["lastPublishedAt"] is None
        assert status["brokerConnections"] == {
            "angelOne": False,
            "upstox": False,
            "binance": False,
        }

    @pytest.mark.asyncio
    async def test_last_published_at_is_set_after_publish(
        self, engine: StreamingEngine
    ) -> None:
        assert engine.get_stream_status()["lastPublishedAt"] is None
        tick = _make_tick()
        await engine.publish_tick(tick)
        last = engine.get_stream_status()["lastPublishedAt"]
        assert last is not None
        # Should be a UTC ISO-8601 string ending with 'Z'
        assert last.endswith("Z")

    @pytest.mark.asyncio
    async def test_subscribed_symbols_populated_after_publish(
        self, engine: StreamingEngine
    ) -> None:
        """Symbols seen in publish_tick calls appear in subscribedSymbols."""
        await engine.publish_tick(_make_tick(symbol="NIFTY"))
        await engine.publish_tick(_make_tick(symbol="BANKNIFTY", ltp=51_000.0))

        status = engine.get_stream_status()
        assert "NIFTY" in status["subscribedSymbols"]
        assert "BANKNIFTY" in status["subscribedSymbols"]

    def test_broker_connections_updated_via_set_broker_connection(
        self, engine: StreamingEngine
    ) -> None:
        engine.set_broker_connection("angelOne", True)
        status = engine.get_stream_status()
        assert status["brokerConnections"]["angelOne"] is True
        assert status["brokerConnections"]["upstox"] is False

    def test_validation_failure_counter_increments(
        self, engine: StreamingEngine
    ) -> None:
        engine.record_validation_failure("ohlcv_invariant")
        engine.record_validation_failure("ohlcv_invariant")
        engine.record_validation_failure("schema_validation")

        status = engine.get_stream_status()
        assert status["validationFailures"]["ohlcv_invariant"] == 2
        assert status["validationFailures"]["schema_validation"] == 1


# ---------------------------------------------------------------------------
# Tests — publish_dataset_ready
# ---------------------------------------------------------------------------


class TestPublishDatasetReady:
    """Tests for StreamingEngine.publish_dataset_ready."""

    @pytest.mark.asyncio
    async def test_sends_to_event_bus(
        self, engine: StreamingEngine, mock_event_bus: MagicMock
    ) -> None:
        """publish_dataset_ready must forward to EventBus.publish_dataset_ready."""
        event = _make_dataset_ready_event()
        msg_id = await engine.publish_dataset_ready(event)

        mock_event_bus.publish_dataset_ready.assert_called_once_with(event)
        assert msg_id == "1705300000456-0"

    @pytest.mark.asyncio
    async def test_returns_empty_string_when_no_event_bus(
        self, dedup_store: DedupStore
    ) -> None:
        """When no EventBus is configured, returns '' without raising."""
        engine = StreamingEngine(event_bus=None, dedup_store=dedup_store)
        event = _make_dataset_ready_event()
        result = await engine.publish_dataset_ready(event)
        assert result == ""

    @pytest.mark.asyncio
    async def test_does_not_affect_ticks_published_counter(
        self, engine: StreamingEngine
    ) -> None:
        """publish_dataset_ready must not increment the ticksPublished counter."""
        event = _make_dataset_ready_event()
        await engine.publish_dataset_ready(event)
        assert engine.get_stream_status()["ticksPublished"] == 0


# ---------------------------------------------------------------------------
# Tests — symbol subscription helpers
# ---------------------------------------------------------------------------


class TestSymbolSubscription:
    """Tests for subscribe_symbol / unsubscribe_symbol helpers."""

    def test_subscribe_adds_symbol(self, engine: StreamingEngine) -> None:
        engine.subscribe_symbol("NIFTY")
        assert "NIFTY" in engine.get_stream_status()["subscribedSymbols"]

    def test_unsubscribe_removes_symbol(self, engine: StreamingEngine) -> None:
        engine.subscribe_symbol("NIFTY")
        engine.unsubscribe_symbol("NIFTY")
        assert "NIFTY" not in engine.get_stream_status()["subscribedSymbols"]

    def test_unsubscribe_nonexistent_symbol_does_not_raise(
        self, engine: StreamingEngine
    ) -> None:
        engine.unsubscribe_symbol("DOESNOTEXIST")  # must not raise

    def test_subscribed_symbols_returned_sorted(
        self, engine: StreamingEngine
    ) -> None:
        engine.subscribe_symbol("BANKNIFTY")
        engine.subscribe_symbol("NIFTY")
        engine.subscribe_symbol("RELIANCE")
        status = engine.get_stream_status()
        assert status["subscribedSymbols"] == sorted(["BANKNIFTY", "NIFTY", "RELIANCE"])
