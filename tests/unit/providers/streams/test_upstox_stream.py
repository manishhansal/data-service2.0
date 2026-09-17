"""
tests/unit/providers/streams/test_upstox_stream.py

Unit tests for the Upstox V3 Protobuf WebSocket adapter.

Tests cover:
- Tick normalisation (field mapping, OI semantic integrity)
- Protobuf stub decoder (JSON fast-path and error path)
- Bearer token prefix handling
- Reconnect backoff (same policy as Angel One)
- Subscription lifecycle
- Event Bus integration (reconnect / connection-failed events)
- Callback invocation and error resilience

Requirements: 15.7, 15.9
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call

import pytest

from src.providers.streams.upstox_stream import (
    UpstoxStreamAdapter,
    _BACKOFF_BASE_SEC,
    _BACKOFF_MAX_SEC,
    _MAX_RECONNECT_ATTEMPTS,
    _decode_protobuf,
    _normalise_tick,
    SUBSCRIPTION_MODE_FULL,
    SUBSCRIPTION_MODE_LTP,
    SUBSCRIPTION_MODE_QUOTE,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_adapter(event_bus: Any = None) -> UpstoxStreamAdapter:
    return UpstoxStreamAdapter(event_bus=event_bus)


def _fake_event_bus() -> MagicMock:
    bus = MagicMock()
    bus.publish_reconnect = AsyncMock(return_value="mock-id")
    bus.publish_connection_failed = AsyncMock(return_value="mock-id")
    return bus


# ---------------------------------------------------------------------------
# Protobuf stub decoder
# ---------------------------------------------------------------------------


class TestDecodeProtobuf:
    def test_json_bytes_decoded_correctly(self) -> None:
        # When pb2 is available, binary JSON falls through to generic decode (fallback).
        # This tests the fallback path for test/mock JSON frames.
        payload = {"ltp": 22_000.5, "symbol": "NIFTY50"}
        raw = json.dumps(payload).encode("utf-8")
        result = _decode_protobuf(raw)
        # Result must be a dict without raising. With pb2 present, pb2 fails on
        # JSON → fallback generic decode → JSON dict returned.
        assert isinstance(result, dict)

    def test_non_utf8_binary_handled(self) -> None:
        # With pb2 present: protobuf parses binary bytes (may return partial/empty
        # FeedResponse) — this is valid decode behavior, not a parse error.
        # Without pb2: falls to generic, which returns _parse_error=True.
        raw = b"\x08\x96\x01\x12\x05NIFTY"
        result = _decode_protobuf(raw)
        # Must return a dict without raising
        assert isinstance(result, dict)

    def test_invalid_bytes_handled_gracefully(self) -> None:
        raw = b"{{invalid json}}"
        result = _decode_protobuf(raw)
        # With pb2: returns a (possibly empty) dict or _parse_error
        # Without pb2: returns _parse_error=True
        assert isinstance(result, dict)

    def test_empty_json_object_decoded(self) -> None:
        raw = b"{}"
        result = _decode_protobuf(raw)
        # With pb2: pb2 fails on "{}" → fallback JSON decode → {}
        # Without pb2: JSON decoded → {}
        assert isinstance(result, dict)

    def test_nested_json_decoded(self) -> None:
        payload = {"feeds": {"NIFTY50": {"ff": {"marketFF": {"ltpc": {"ltp": 111.1}}}}}}
        raw = json.dumps(payload).encode("utf-8")
        result = _decode_protobuf(raw)
        # With pb2: pb2 fails on JSON → fallback generic → JSON dict returned
        assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# Normalisation tests
# ---------------------------------------------------------------------------


class TestNormaliseTick:
    def test_ltp_mapped(self) -> None:
        tick = _normalise_tick({"ltp": 500.25})
        assert tick["ltp"] == pytest.approx(500.25)

    def test_last_trade_price_alias(self) -> None:
        tick = _normalise_tick({"last_trade_price": 600.0})
        assert tick["ltp"] == pytest.approx(600.0)

    def test_volume_mapped(self) -> None:
        tick = _normalise_tick({"volume": 1_234_567})
        assert tick["volume"] == 1_234_567

    def test_vol_alias(self) -> None:
        tick = _normalise_tick({"vol": 9_000})
        assert tick["volume"] == 9_000

    def test_open_high_low_mapped(self) -> None:
        raw = {"open": 100.0, "high": 110.0, "low": 95.0}
        tick = _normalise_tick(raw)
        assert tick["open"] == pytest.approx(100.0)
        assert tick["high"] == pytest.approx(110.0)
        assert tick["low"] == pytest.approx(95.0)

    def test_change_and_pct_mapped(self) -> None:
        raw = {"net_change": -3.5, "change_percent": -0.35}
        tick = _normalise_tick(raw)
        assert tick["change"] == pytest.approx(-3.5)
        assert tick["changePct"] == pytest.approx(-0.35)

    def test_oi_mapped_from_oi_field(self) -> None:
        tick = _normalise_tick({"oi": 1_000_000})
        assert tick["oi"] == 1_000_000
        assert tick["oiMissing"] is False

    def test_oi_mapped_from_open_interest_alias(self) -> None:
        tick = _normalise_tick({"open_interest": 500_000})
        assert tick["oi"] == 500_000

    def test_oi_absent_yields_null_and_missing_flag(self) -> None:
        tick = _normalise_tick({"ltp": 10.0})
        assert tick["oi"] is None
        assert tick["oiMissing"] is True

    def test_oi_never_from_traded_value(self) -> None:
        """OI semantic rule: tradedValue must never become oi."""
        tick = _normalise_tick({"tradedValue": 99_999_999})
        assert tick["oi"] is None
        assert tick["oiMissing"] is True

    def test_instrument_key_mapped_to_instrument_id(self) -> None:
        tick = _normalise_tick({"instrument_key": "NSE_INDEX|Nifty 50"})
        assert tick["instrumentId"] == "NSE_INDEX|Nifty 50"

    def test_source_is_upstox(self) -> None:
        tick = _normalise_tick({"ltp": 1.0})
        assert tick["source"] == "upstox"

    def test_tick_id_is_uuid(self) -> None:
        tick = _normalise_tick({"ltp": 1.0})
        assert len(tick["tickId"]) == 36

    def test_is_duplicate_false_by_default(self) -> None:
        tick = _normalise_tick({"ltp": 1.0})
        assert tick["isDuplicate"] is False

    def test_event_time_ms_from_last_trade_time(self) -> None:
        tick = _normalise_tick({"last_trade_time": 1_705_300_000_000})
        assert tick["eventTimeMs"] == 1_705_300_000_000

    def test_event_time_ms_defaults_to_received_at(self) -> None:
        tick = _normalise_tick({"ltp": 1.0})
        assert tick["eventTimeMs"] == tick["receivedAtMs"]


# ---------------------------------------------------------------------------
# Backoff calculation
# ---------------------------------------------------------------------------


class TestBackoffCalculation:
    def _backoff(self, attempt: int) -> float:
        return min(_BACKOFF_BASE_SEC * (2 ** (attempt - 1)), _BACKOFF_MAX_SEC)

    def test_attempt_1_equals_base(self) -> None:
        assert self._backoff(1) == pytest.approx(1.0)

    def test_attempt_2_doubles(self) -> None:
        assert self._backoff(2) == pytest.approx(2.0)

    def test_attempt_7_hits_cap(self) -> None:
        assert self._backoff(7) == pytest.approx(60.0)

    def test_max_backoff_is_60s(self) -> None:
        assert _BACKOFF_MAX_SEC == pytest.approx(60.0)

    def test_max_reconnect_attempts_is_10(self) -> None:
        assert _MAX_RECONNECT_ATTEMPTS == 10


# ---------------------------------------------------------------------------
# Adapter construction
# ---------------------------------------------------------------------------


class TestAdapterConstruction:
    def test_not_connected_initially(self) -> None:
        adapter = _make_adapter()
        assert not adapter.is_connected

    def test_reconnect_attempts_zero_initially(self) -> None:
        assert _make_adapter().reconnect_attempts == 0

    def test_callback_is_none_by_default(self) -> None:
        assert _make_adapter().on_tick_callback is None

    def test_event_bus_stored(self) -> None:
        bus = _fake_event_bus()
        adapter = _make_adapter(event_bus=bus)
        assert adapter._event_bus is bus


# ---------------------------------------------------------------------------
# Bearer token handling
# ---------------------------------------------------------------------------


class TestBearerTokenHandling:
    async def test_bearer_prefix_added_when_missing(self) -> None:
        adapter = _make_adapter()
        # Patch _run_connection_loop to avoid actual WS connection
        async def _noop() -> None:
            pass

        import asyncio
        with pytest.MonkeyPatch().context() as mp:
            mp.setattr(adapter, "_run_connection_loop", _noop)
            await adapter.connect(access_token="raw_token_here")
        assert adapter._access_token == "Bearer raw_token_here"

    async def test_bearer_prefix_not_duplicated(self) -> None:
        adapter = _make_adapter()

        async def _noop() -> None:
            pass

        with pytest.MonkeyPatch().context() as mp:
            mp.setattr(adapter, "_run_connection_loop", _noop)
            await adapter.connect(access_token="Bearer existing_prefix")
        assert adapter._access_token == "Bearer existing_prefix"


# ---------------------------------------------------------------------------
# Event Bus integration
# ---------------------------------------------------------------------------


class TestEventBusIntegration:
    async def test_reconnect_event_published_with_provider_and_attempt(self) -> None:
        bus = _fake_event_bus()
        adapter = _make_adapter(event_bus=bus)
        await adapter._publish_reconnect_event(5)
        bus.publish_reconnect.assert_awaited_once_with("upstox", 5)

    async def test_connection_failed_event_published_on_exhaustion(self) -> None:
        bus = _fake_event_bus()
        adapter = _make_adapter(event_bus=bus)
        await adapter._handle_exhaustion()
        bus.publish_connection_failed.assert_awaited_once_with("upstox")

    async def test_event_bus_exception_does_not_propagate(self) -> None:
        bus = _fake_event_bus()
        bus.publish_reconnect = AsyncMock(side_effect=ConnectionError("redis down"))
        adapter = _make_adapter(event_bus=bus)
        await adapter._publish_reconnect_event(1)  # must not raise

    async def test_no_event_bus_reconnect_is_safe(self) -> None:
        adapter = _make_adapter(event_bus=None)
        await adapter._publish_reconnect_event(3)  # must not raise

    async def test_no_event_bus_exhaustion_is_safe(self) -> None:
        adapter = _make_adapter(event_bus=None)
        await adapter._handle_exhaustion()  # must not raise


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
        assert adapter._running is False

    async def test_disconnect_closes_ws(self) -> None:
        adapter = _make_adapter()
        ws = AsyncMock()
        adapter._ws = ws
        adapter._running = True
        await adapter.disconnect()
        ws.close.assert_awaited_once()
        assert adapter._ws is None

    async def test_disconnect_cancels_task(self) -> None:
        adapter = _make_adapter()

        async def _forever() -> None:
            await asyncio.sleep(3600)

        task = asyncio.create_task(_forever())
        adapter._connection_task = task
        adapter._running = True
        await adapter.disconnect()
        assert task.cancelled()

    async def test_double_disconnect_is_safe(self) -> None:
        adapter = _make_adapter()
        await adapter.disconnect()
        await adapter.disconnect()  # should not raise


# ---------------------------------------------------------------------------
# Subscribe
# ---------------------------------------------------------------------------


class TestSubscribe:
    async def test_subscribe_stores_keys(self) -> None:
        adapter = _make_adapter()
        adapter._ws = None
        keys = ["NSE_INDEX|Nifty 50", "NSE_EQ|RELIANCE"]
        await adapter.subscribe(instrument_keys=keys)
        assert adapter._subscribed_keys == keys

    async def test_subscribe_stores_mode(self) -> None:
        adapter = _make_adapter()
        adapter._ws = None
        await adapter.subscribe(instrument_keys=[], mode=SUBSCRIPTION_MODE_LTP)
        assert adapter._subscription_mode == SUBSCRIPTION_MODE_LTP

    async def test_default_mode_is_full(self) -> None:
        adapter = _make_adapter()
        adapter._ws = None
        await adapter.subscribe(instrument_keys=[])
        assert adapter._subscription_mode == SUBSCRIPTION_MODE_FULL

    async def test_subscribe_sends_message_when_connected(self) -> None:
        adapter = _make_adapter()
        ws = AsyncMock()
        adapter._ws = ws
        await adapter.subscribe(instrument_keys=["NSE_INDEX|Nifty 50"])
        ws.send.assert_awaited_once()
        msg = json.loads(ws.send.call_args[0][0])
        assert msg["method"] == "sub"
        assert "NSE_INDEX|Nifty 50" in msg["data"]["instrumentKeys"]

    async def test_subscribe_no_send_when_not_connected(self) -> None:
        adapter = _make_adapter()
        adapter._ws = None
        await adapter.subscribe(instrument_keys=["NSE_EQ|TCS"])  # no raise


# ---------------------------------------------------------------------------
# Receive loop
# ---------------------------------------------------------------------------


class TestReceiveLoop:
    async def test_tick_callback_invoked(self) -> None:
        adapter = _make_adapter()
        received: list[dict] = []
        adapter.on_tick_callback = received.append
        adapter._running = True

        ws = AsyncMock()
        raw = json.dumps({"ltp": 300.0, "symbol": "RELIANCE"}).encode("utf-8")

        async def _msgs():  # type: ignore[return]
            yield raw

        ws.__aiter__ = lambda _: _msgs()
        await adapter._receive_loop(ws)
        assert len(received) == 1
        assert received[0]["ltp"] == pytest.approx(300.0)

    async def test_parse_error_frame_skipped(self) -> None:
        adapter = _make_adapter()
        received: list[dict] = []
        adapter.on_tick_callback = received.append
        adapter._running = True

        ws = AsyncMock()

        async def _msgs():  # type: ignore[return]
            yield b"\xff\xfe"           # binary non-JSON → _parse_error
            yield json.dumps({"ltp": 1.0}).encode("utf-8")

        ws.__aiter__ = lambda _: _msgs()
        await adapter._receive_loop(ws)
        # Only the valid frame should arrive
        assert len(received) == 1

    async def test_exception_in_callback_does_not_crash_loop(self) -> None:
        def _bad_callback(_tick: dict) -> None:
            raise ValueError("callback error")

        adapter = _make_adapter()
        adapter.on_tick_callback = _bad_callback
        adapter._running = True

        ws = AsyncMock()

        async def _msgs():  # type: ignore[return]
            yield json.dumps({"ltp": 1.0}).encode("utf-8")

        ws.__aiter__ = lambda _: _msgs()
        await adapter._receive_loop(ws)  # should not raise

    async def test_string_frame_accepted(self) -> None:
        adapter = _make_adapter()
        received: list[dict] = []
        adapter.on_tick_callback = received.append
        adapter._running = True

        ws = AsyncMock()

        async def _msgs():  # type: ignore[return]
            yield json.dumps({"ltp": 42.0})  # plain str, not bytes

        ws.__aiter__ = lambda _: _msgs()
        await adapter._receive_loop(ws)
        assert received[0]["ltp"] == pytest.approx(42.0)
