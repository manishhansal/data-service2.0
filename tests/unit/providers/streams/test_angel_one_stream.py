"""
tests/unit/providers/streams/test_angel_one_stream.py

Unit tests for the Angel One SmartStream WebSocket adapter.

Tests cover:
- Tick normalisation (field mapping, OI semantic integrity)
- Reconnect backoff calculation
- Connection lifecycle (connect / disconnect / subscribe)
- Reconnect-event and connection-failed-event publication
- Reconnect exhaustion behaviour
- Callback invocation on tick receipt

Requirements: 15.9
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

from src.providers.streams.angel_one_stream import (
    AngelOneStreamAdapter,
    _BACKOFF_BASE_SEC,
    _BACKOFF_MAX_SEC,
    _MAX_RECONNECT_ATTEMPTS,
    _normalise_tick,
    SUBSCRIPTION_MODE_FULL,
    SUBSCRIPTION_MODE_LTP,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_adapter(event_bus: Any = None) -> AngelOneStreamAdapter:
    return AngelOneStreamAdapter(event_bus=event_bus)


def _fake_event_bus() -> MagicMock:
    bus = MagicMock()
    bus.publish_reconnect = AsyncMock(return_value="mock-id")
    bus.publish_connection_failed = AsyncMock(return_value="mock-id")
    bus.publish_tick = AsyncMock(return_value="mock-id")
    return bus


# ---------------------------------------------------------------------------
# Normalisation tests
# ---------------------------------------------------------------------------


class TestNormaliseTick:
    """Unit tests for the _normalise_tick helper."""

    def test_ltp_field_mapped(self) -> None:
        raw = {"ltp": 22150.50, "symbol": "NIFTY"}
        tick = _normalise_tick(raw)
        assert tick["ltp"] == 22150.50

    def test_last_traded_price_alias(self) -> None:
        raw = {"last_traded_price": 1234567}  # paise as int
        tick = _normalise_tick(raw)
        # int ltp is divided by 100 to convert paise to rupees
        assert tick["ltp"] == pytest.approx(12345.67)

    def test_volume_mapped(self) -> None:
        raw = {"volume_trade_for_the_day": 9876543}
        tick = _normalise_tick(raw)
        assert tick["volume"] == 9876543

    def test_change_and_change_pct_mapped(self) -> None:
        raw = {"net_change": 45.25, "percent_change": 0.204}
        tick = _normalise_tick(raw)
        assert tick["change"] == 45.25
        assert tick["changePct"] == pytest.approx(0.204)

    def test_open_interest_mapped_to_oi(self) -> None:
        raw = {"open_interest": 500000, "symbol": "BANKNIFTY"}
        tick = _normalise_tick(raw)
        assert tick["oi"] == 500000
        assert tick["oiMissing"] is False

    def test_oi_absent_yields_null_and_oi_missing_flag(self) -> None:
        """OI semantic rule: absent OI → null + oiMissing: true."""
        raw = {"ltp": 100.0}
        tick = _normalise_tick(raw)
        assert tick["oi"] is None
        assert tick["oiMissing"] is True

    def test_oi_never_populated_from_traded_value(self) -> None:
        """OI must never be set from tradedValue — core semantic rule."""
        raw = {"tradedValue": 999_000_000}  # only tradedValue, no OI
        tick = _normalise_tick(raw)
        assert tick["oi"] is None
        assert tick["oiMissing"] is True

    def test_tick_id_is_uuid_string(self) -> None:
        raw = {"ltp": 10.0}
        tick = _normalise_tick(raw)
        assert isinstance(tick["tickId"], str)
        assert len(tick["tickId"]) == 36  # UUID v4 canonical form

    def test_source_is_angel_one(self) -> None:
        tick = _normalise_tick({"ltp": 1.0})
        assert tick["source"] == "angel_one"

    def test_received_at_ms_is_int(self) -> None:
        tick = _normalise_tick({"ltp": 1.0})
        assert isinstance(tick["receivedAtMs"], int)
        assert tick["receivedAtMs"] > 0

    def test_is_duplicate_defaults_to_false(self) -> None:
        tick = _normalise_tick({"ltp": 1.0})
        assert tick["isDuplicate"] is False

    def test_event_time_ms_from_last_trade_time(self) -> None:
        # Use the raw SmartStream field name (mapped to canonical `lastTradeTime`)
        raw = {"last_traded_time": 1_705_300_000_123}
        tick = _normalise_tick(raw)
        assert tick["eventTimeMs"] == 1_705_300_000_123

    def test_event_time_ms_falls_back_to_received_at(self) -> None:
        tick = _normalise_tick({"ltp": 1.0})
        assert tick["eventTimeMs"] == tick["receivedAtMs"]

    def test_total_buy_sell_qty_mapped(self) -> None:
        raw = {"total_buy_quantity": 12345, "total_sell_quantity": 67890}
        tick = _normalise_tick(raw)
        assert tick["totalBuyQty"] == 12345
        assert tick["totalSellQty"] == 67890

    def test_binary_frame_decoded_as_json(self) -> None:
        """Ensure that if caller JSON-parses bytes first, normalise still works."""
        raw_bytes = json.dumps({"ltp": 555.5, "symbol": "NIFTY"}).encode("utf-8")
        parsed = json.loads(raw_bytes)
        tick = _normalise_tick(parsed)
        assert tick["ltp"] == 555.5


# ---------------------------------------------------------------------------
# Backoff calculation
# ---------------------------------------------------------------------------


class TestBackoffCalculation:
    """Verify the exponential backoff formula independently of the adapter."""

    def _backoff(self, attempt: int) -> float:
        return min(_BACKOFF_BASE_SEC * (2 ** (attempt - 1)), _BACKOFF_MAX_SEC)

    def test_attempt_1_is_base(self) -> None:
        assert self._backoff(1) == pytest.approx(1.0)

    def test_attempt_2_doubles(self) -> None:
        assert self._backoff(2) == pytest.approx(2.0)

    def test_attempt_3_doubles_again(self) -> None:
        assert self._backoff(3) == pytest.approx(4.0)

    def test_attempt_7_hits_cap(self) -> None:
        # 2^6 = 64 > 60, so should clamp to 60
        assert self._backoff(7) == pytest.approx(60.0)

    def test_high_attempts_stay_at_cap(self) -> None:
        for attempt in range(7, 20):
            assert self._backoff(attempt) == pytest.approx(60.0)

    def test_max_reconnect_attempts_is_10(self) -> None:
        assert _MAX_RECONNECT_ATTEMPTS == 10

    def test_max_backoff_is_60(self) -> None:
        assert _BACKOFF_MAX_SEC == pytest.approx(60.0)


# ---------------------------------------------------------------------------
# Adapter construction
# ---------------------------------------------------------------------------


class TestAdapterConstruction:
    def test_initial_state_not_connected(self) -> None:
        adapter = _make_adapter()
        assert not adapter.is_connected

    def test_initial_reconnect_attempts_zero(self) -> None:
        adapter = _make_adapter()
        assert adapter.reconnect_attempts == 0

    def test_no_callback_by_default(self) -> None:
        adapter = _make_adapter()
        assert adapter.on_tick_callback is None

    def test_event_bus_stored(self) -> None:
        bus = _fake_event_bus()
        adapter = _make_adapter(event_bus=bus)
        assert adapter._event_bus is bus


# ---------------------------------------------------------------------------
# Reconnect and exhaustion events
# ---------------------------------------------------------------------------


class TestReconnectEvents:
    """Verify that the adapter publishes events to the Event Bus correctly."""

    async def test_reconnect_event_published_on_each_attempt(self) -> None:
        bus = _fake_event_bus()
        adapter = _make_adapter(event_bus=bus)
        await adapter._publish_reconnect_event(3)
        bus.publish_reconnect.assert_awaited_once_with("angel_one", 3)

    async def test_reconnect_event_attempt_number_passed(self) -> None:
        bus = _fake_event_bus()
        adapter = _make_adapter(event_bus=bus)
        for n in (1, 2, 5, 10):
            await adapter._publish_reconnect_event(n)
        calls = bus.publish_reconnect.await_args_list
        assert calls[0] == call("angel_one", 1)
        assert calls[3] == call("angel_one", 10)

    async def test_connection_failed_event_published_on_exhaustion(self) -> None:
        bus = _fake_event_bus()
        adapter = _make_adapter(event_bus=bus)
        await adapter._handle_exhaustion()
        bus.publish_connection_failed.assert_awaited_once_with("angel_one")

    async def test_reconnect_event_suppressed_when_no_event_bus(self) -> None:
        """No error raised when event_bus is None."""
        adapter = _make_adapter(event_bus=None)
        # Should not raise
        await adapter._publish_reconnect_event(1)

    async def test_connection_failed_suppressed_when_no_event_bus(self) -> None:
        adapter = _make_adapter(event_bus=None)
        await adapter._handle_exhaustion()  # should not raise

    async def test_event_bus_error_does_not_propagate(self) -> None:
        """Event Bus failures must not crash the adapter."""
        bus = _fake_event_bus()
        bus.publish_reconnect = AsyncMock(side_effect=RuntimeError("redis down"))
        adapter = _make_adapter(event_bus=bus)
        # Should swallow the exception
        await adapter._publish_reconnect_event(1)

    async def test_connection_failed_event_bus_error_does_not_propagate(self) -> None:
        bus = _fake_event_bus()
        bus.publish_connection_failed = AsyncMock(side_effect=RuntimeError("redis down"))
        adapter = _make_adapter(event_bus=bus)
        await adapter._handle_exhaustion()  # should not raise


# ---------------------------------------------------------------------------
# Disconnect
# ---------------------------------------------------------------------------


class TestDisconnect:
    async def test_disconnect_sets_running_false(self) -> None:
        adapter = _make_adapter()
        adapter._running = True
        adapter._ws = None
        adapter._connection_task = None
        await adapter.disconnect()
        assert not adapter._running

    async def test_disconnect_closes_websocket(self) -> None:
        adapter = _make_adapter()
        mock_ws = AsyncMock()
        mock_ws.close = AsyncMock()
        adapter._ws = mock_ws
        adapter._running = True
        await adapter.disconnect()
        mock_ws.close.assert_awaited_once()
        assert adapter._ws is None

    async def test_disconnect_cancels_connection_task(self) -> None:
        adapter = _make_adapter()
        # Create a task that will run forever
        async def _forever() -> None:
            await asyncio.sleep(3600)

        task = asyncio.create_task(_forever())
        adapter._connection_task = task
        adapter._running = True
        await adapter.disconnect()
        assert task.cancelled()

    async def test_disconnect_when_already_disconnected_is_safe(self) -> None:
        adapter = _make_adapter()
        await adapter.disconnect()  # should not raise


# ---------------------------------------------------------------------------
# Subscribe
# ---------------------------------------------------------------------------


class TestSubscribe:
    async def test_subscribe_stores_tokens(self) -> None:
        adapter = _make_adapter()
        adapter._ws = None  # not connected yet
        await adapter.subscribe(tokens=["256265", "99926000"])
        assert adapter._subscribed_tokens == ["256265", "99926000"]

    async def test_subscribe_stores_mode(self) -> None:
        adapter = _make_adapter()
        adapter._ws = None
        await adapter.subscribe(tokens=["256265"], mode=SUBSCRIPTION_MODE_LTP)
        assert adapter._subscription_mode == SUBSCRIPTION_MODE_LTP

    async def test_subscribe_default_mode_is_full(self) -> None:
        adapter = _make_adapter()
        adapter._ws = None
        await adapter.subscribe(tokens=["256265"])
        assert adapter._subscription_mode == SUBSCRIPTION_MODE_FULL

    async def test_subscribe_sends_message_when_connected(self) -> None:
        adapter = _make_adapter()
        mock_ws = AsyncMock()
        mock_ws.send = AsyncMock()
        adapter._ws = mock_ws
        await adapter.subscribe(tokens=["123456"])
        mock_ws.send.assert_awaited_once()
        sent_data = json.loads(mock_ws.send.call_args[0][0])
        assert sent_data["action"] == 1
        assert "123456" in sent_data["params"]["tokenList"][0]["tokens"]

    async def test_subscribe_skips_send_when_not_connected(self) -> None:
        """No send when ws is None — tokens stored for replay on connect."""
        adapter = _make_adapter()
        adapter._ws = None
        await adapter.subscribe(tokens=["999"])  # should not raise


# ---------------------------------------------------------------------------
# Tick callback
# ---------------------------------------------------------------------------


class TestTickCallback:
    async def test_callback_invoked_on_receive_loop(self) -> None:
        adapter = _make_adapter()
        received: list[dict] = []
        adapter.on_tick_callback = received.append

        mock_ws = AsyncMock()
        raw_tick = json.dumps({"ltp": 100.0, "symbol": "NIFTY"})

        # Simulate two messages then stop
        async def _messages():  # type: ignore[return]
            yield raw_tick
            yield raw_tick

        mock_ws.__aiter__ = lambda _: _messages()

        adapter._running = True
        await adapter._receive_loop(mock_ws)
        assert len(received) == 2
        assert received[0]["ltp"] == pytest.approx(100.0)

    async def test_no_callback_does_not_raise(self) -> None:
        adapter = _make_adapter()
        adapter.on_tick_callback = None  # no callback set

        mock_ws = AsyncMock()
        raw_tick = json.dumps({"ltp": 200.0})

        async def _messages():  # type: ignore[return]
            yield raw_tick

        mock_ws.__aiter__ = lambda _: _messages()
        adapter._running = True
        await adapter._receive_loop(mock_ws)  # should not raise

    async def test_invalid_json_does_not_crash_receive_loop(self) -> None:
        adapter = _make_adapter()
        received: list[dict] = []
        adapter.on_tick_callback = received.append

        mock_ws = AsyncMock()

        async def _messages():  # type: ignore[return]
            yield "{{not valid json}}"
            yield json.dumps({"ltp": 50.0})  # good message follows

        mock_ws.__aiter__ = lambda _: _messages()
        adapter._running = True
        await adapter._receive_loop(mock_ws)
        # The good message should still arrive
        assert len(received) == 1

    async def test_bytes_frame_decoded_as_json(self) -> None:
        adapter = _make_adapter()
        received: list[dict] = []
        adapter.on_tick_callback = received.append

        mock_ws = AsyncMock()

        async def _messages():  # type: ignore[return]
            yield json.dumps({"ltp": 75.5}).encode("utf-8")

        mock_ws.__aiter__ = lambda _: _messages()
        adapter._running = True
        await adapter._receive_loop(mock_ws)
        assert received[0]["ltp"] == pytest.approx(75.5)
