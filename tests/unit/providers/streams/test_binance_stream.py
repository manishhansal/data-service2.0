"""
tests/unit/providers/streams/test_binance_stream.py

Unit tests for the Binance WebSocket adapter.

Tests cover:
- Stream URL construction (_build_stream_url)
- Tick normalisation from mini-ticker frames
- Combined-stream frame routing (data envelope unwrapping)
- Reconnect backoff (base 1s, max 30s — shorter than Indian providers)
- Heartbeat task lifecycle
- Event Bus tick publication
- Callback invocation and error resilience
- Disconnect lifecycle

Requirements: 13.5, 13.6, 13.11
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from src.providers.streams.binance_stream import (
    BinanceStreamAdapter,
    _BACKOFF_BASE_SEC,
    _BACKOFF_MAX_SEC,
    _HEARTBEAT_INTERVAL_SEC,
    _WS_BASE_URL,
    _normalise_mini_ticker,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_adapter(event_bus: Any = None) -> BinanceStreamAdapter:
    return BinanceStreamAdapter(event_bus=event_bus)


def _fake_event_bus() -> MagicMock:
    bus = MagicMock()
    bus.publish_tick = AsyncMock(return_value="mock-id")
    return bus


def _mini_ticker_frame(symbol: str, **overrides: Any) -> dict:
    """Build a Binance combined-stream mini-ticker frame."""
    data = {
        "e": "24hrMiniTicker",
        "E": 1_705_300_000_123,
        "s": symbol,
        "c": "45000.00",
        "o": "44000.00",
        "h": "46000.00",
        "l": "43500.00",
        "v": "1234.56",
        "q": "55_670_000.00",
    }
    data.update(overrides)
    return {"stream": f"{symbol.lower()}@miniTicker", "data": data}


# ---------------------------------------------------------------------------
# Stream URL construction
# ---------------------------------------------------------------------------


class TestBuildStreamUrl:
    def test_single_symbol(self) -> None:
        url = BinanceStreamAdapter._build_stream_url(["BTCUSDT"])
        assert url == f"{_WS_BASE_URL}?streams=btcusdt@miniTicker"

    def test_multiple_symbols_joined_with_slash(self) -> None:
        url = BinanceStreamAdapter._build_stream_url(["BTCUSDT", "ETHUSDT", "SOLUSDT"])
        assert "btcusdt@miniTicker" in url
        assert "ethusdt@miniTicker" in url
        assert "solusdt@miniTicker" in url
        # slash-separated
        assert "/".join(["btcusdt@miniTicker", "ethusdt@miniTicker", "solusdt@miniTicker"]) in url

    def test_symbols_lowercased_in_url(self) -> None:
        url = BinanceStreamAdapter._build_stream_url(["BTCUSDT"])
        assert "BTCUSDT" not in url
        assert "btcusdt" in url

    def test_base_url_prefix(self) -> None:
        url = BinanceStreamAdapter._build_stream_url(["ETHUSDT"])
        assert url.startswith(_WS_BASE_URL)

    def test_empty_symbols_produces_empty_streams(self) -> None:
        url = BinanceStreamAdapter._build_stream_url([])
        assert url == f"{_WS_BASE_URL}?streams="


# ---------------------------------------------------------------------------
# Mini-ticker normalisation
# ---------------------------------------------------------------------------


class TestNormaliseMiniTicker:
    def _data(self, symbol: str = "BTCUSDT", **kw: Any) -> dict:
        d = {
            "e": "24hrMiniTicker",
            "E": 1_705_300_000_000,
            "s": symbol,
            "c": "45000.0",
            "o": "44000.0",
            "h": "46000.0",
            "l": "43500.0",
            "v": "100.5",
            "q": "4_500_000.0",
        }
        d.update(kw)
        return d

    def test_ltp_from_close_field(self) -> None:
        tick = _normalise_mini_ticker(self._data(c="45000.50"))
        assert tick["ltp"] == pytest.approx(45000.50)

    def test_open_high_low_mapped(self) -> None:
        tick = _normalise_mini_ticker(self._data(o="44000", h="46000", l="43500"))
        assert tick["open"] == pytest.approx(44000.0)
        assert tick["high"] == pytest.approx(46000.0)
        assert tick["low"] == pytest.approx(43500.0)

    def test_volume_mapped_from_v(self) -> None:
        tick = _normalise_mini_ticker(self._data(v="1234.56"))
        assert tick["volume"] == pytest.approx(1234.56)

    def test_quote_volume_mapped_from_q(self) -> None:
        tick = _normalise_mini_ticker(self._data(q="55670000.0"))
        assert tick["quoteVolume"] == pytest.approx(55_670_000.0)

    def test_event_time_ms_from_E(self) -> None:
        tick = _normalise_mini_ticker(self._data(E=1_705_300_000_123))
        assert tick["eventTimeMs"] == 1_705_300_000_123

    def test_symbol_uppercased(self) -> None:
        tick = _normalise_mini_ticker(self._data(s="btcusdt"))
        assert tick["symbol"] == "BTCUSDT"

    def test_source_is_binance(self) -> None:
        tick = _normalise_mini_ticker(self._data())
        assert tick["source"] == "binance"

    def test_exchange_is_binance(self) -> None:
        tick = _normalise_mini_ticker(self._data())
        assert tick["exchange"] == "BINANCE"

    def test_oi_always_null(self) -> None:
        """Binance mini-ticker never carries Indian-market OI."""
        tick = _normalise_mini_ticker(self._data())
        assert tick["oi"] is None
        assert tick["oiMissing"] is True

    def test_is_duplicate_false(self) -> None:
        tick = _normalise_mini_ticker(self._data())
        assert tick["isDuplicate"] is False

    def test_tick_id_is_uuid(self) -> None:
        tick = _normalise_mini_ticker(self._data())
        assert len(tick["tickId"]) == 36

    def test_received_at_ms_is_int(self) -> None:
        tick = _normalise_mini_ticker(self._data())
        assert isinstance(tick["receivedAtMs"], int)
        assert tick["receivedAtMs"] > 0


# ---------------------------------------------------------------------------
# Backoff constants
# ---------------------------------------------------------------------------


class TestBackoffConstants:
    """Binance uses a shorter cap (30s) than Indian providers (60s)."""

    def _backoff(self, attempt: int) -> float:
        return min(_BACKOFF_BASE_SEC * (2 ** (attempt - 1)), _BACKOFF_MAX_SEC)

    def test_base_is_1s(self) -> None:
        assert _BACKOFF_BASE_SEC == pytest.approx(1.0)

    def test_cap_is_30s(self) -> None:
        assert _BACKOFF_MAX_SEC == pytest.approx(30.0)

    def test_attempt_1_equals_1s(self) -> None:
        assert self._backoff(1) == pytest.approx(1.0)

    def test_attempt_2_equals_2s(self) -> None:
        assert self._backoff(2) == pytest.approx(2.0)

    def test_attempt_5_hits_cap(self) -> None:
        # 2^4 = 16 < 30, 2^5 = 32 > 30
        assert self._backoff(6) == pytest.approx(30.0)

    def test_high_attempts_stay_at_cap(self) -> None:
        for attempt in range(6, 20):
            assert self._backoff(attempt) == pytest.approx(30.0)


# ---------------------------------------------------------------------------
# Heartbeat constant
# ---------------------------------------------------------------------------


class TestHeartbeatConstant:
    def test_heartbeat_interval_is_30s(self) -> None:
        assert _HEARTBEAT_INTERVAL_SEC == pytest.approx(30.0)


# ---------------------------------------------------------------------------
# Adapter construction
# ---------------------------------------------------------------------------


class TestAdapterConstruction:
    def test_not_connected_initially(self) -> None:
        assert not _make_adapter().is_connected

    def test_reconnect_attempts_zero(self) -> None:
        assert _make_adapter().reconnect_attempts == 0

    def test_callback_none_by_default(self) -> None:
        assert _make_adapter().on_tick_callback is None

    def test_event_bus_stored(self) -> None:
        bus = _fake_event_bus()
        adapter = _make_adapter(event_bus=bus)
        assert adapter._event_bus is bus


# ---------------------------------------------------------------------------
# Receive loop — combined-stream frame routing
# ---------------------------------------------------------------------------


class TestReceiveLoop:
    async def test_mini_ticker_event_triggers_callback(self) -> None:
        adapter = _make_adapter()
        received: list[dict] = []
        adapter.on_tick_callback = received.append
        adapter._running = True

        ws = AsyncMock()
        frame = _mini_ticker_frame("BTCUSDT")

        async def _msgs():  # type: ignore[return]
            yield json.dumps(frame)

        ws.__aiter__ = lambda _: _msgs()
        await adapter._receive_loop(ws)
        assert len(received) == 1
        assert received[0]["symbol"] == "BTCUSDT"
        assert received[0]["ltp"] == pytest.approx(45000.0)

    async def test_non_mini_ticker_event_skipped(self) -> None:
        adapter = _make_adapter()
        received: list[dict] = []
        adapter.on_tick_callback = received.append
        adapter._running = True

        ws = AsyncMock()
        frame = {
            "stream": "btcusdt@depth",
            "data": {"e": "depthUpdate", "s": "BTCUSDT"},
        }

        async def _msgs():  # type: ignore[return]
            yield json.dumps(frame)

        ws.__aiter__ = lambda _: _msgs()
        await adapter._receive_loop(ws)
        # Depth events should be ignored
        assert len(received) == 0

    async def test_multiple_symbols_dispatched(self) -> None:
        adapter = _make_adapter()
        received: list[dict] = []
        adapter.on_tick_callback = received.append
        adapter._running = True

        ws = AsyncMock()
        btc_frame = _mini_ticker_frame("BTCUSDT", **{"data": {
            "e": "24hrMiniTicker", "E": 123, "s": "BTCUSDT",
            "c": "45000", "o": "44000", "h": "46000", "l": "43000", "v": "10", "q": "450000",
        }})
        eth_frame = _mini_ticker_frame("ETHUSDT", **{"data": {
            "e": "24hrMiniTicker", "E": 124, "s": "ETHUSDT",
            "c": "3000", "o": "2900", "h": "3100", "l": "2850", "v": "100", "q": "300000",
        }})

        async def _msgs():  # type: ignore[return]
            yield json.dumps(btc_frame)
            yield json.dumps(eth_frame)

        ws.__aiter__ = lambda _: _msgs()
        await adapter._receive_loop(ws)
        symbols = {t["symbol"] for t in received}
        assert "BTCUSDT" in symbols
        assert "ETHUSDT" in symbols

    async def test_invalid_json_skipped(self) -> None:
        adapter = _make_adapter()
        received: list[dict] = []
        adapter.on_tick_callback = received.append
        adapter._running = True

        ws = AsyncMock()

        async def _msgs():  # type: ignore[return]
            yield "{{bad json}}"
            yield json.dumps(_mini_ticker_frame("BTCUSDT"))

        ws.__aiter__ = lambda _: _msgs()
        await adapter._receive_loop(ws)
        # Good frame still arrives
        assert len(received) == 1

    async def test_callback_exception_does_not_crash_loop(self) -> None:
        def _explode(_: dict) -> None:
            raise RuntimeError("oops")

        adapter = _make_adapter()
        adapter.on_tick_callback = _explode
        adapter._running = True

        ws = AsyncMock()

        async def _msgs():  # type: ignore[return]
            yield json.dumps(_mini_ticker_frame("SOLUSDT"))

        ws.__aiter__ = lambda _: _msgs()
        await adapter._receive_loop(ws)  # should not raise

    async def test_bytes_frame_decoded(self) -> None:
        adapter = _make_adapter()
        received: list[dict] = []
        adapter.on_tick_callback = received.append
        adapter._running = True

        ws = AsyncMock()
        raw = json.dumps(_mini_ticker_frame("BTCUSDT")).encode("utf-8")

        async def _msgs():  # type: ignore[return]
            yield raw

        ws.__aiter__ = lambda _: _msgs()
        await adapter._receive_loop(ws)
        assert len(received) == 1

    async def test_error_not_exposed_to_consumer(self) -> None:
        """Raw WebSocket error must not propagate to consumer (Req 13.6)."""
        adapter = _make_adapter()
        adapter._running = True

        ws = AsyncMock()

        async def _msgs():  # type: ignore[return]
            yield "{{invalid}}"  # triggers exception inside loop

        ws.__aiter__ = lambda _: _msgs()
        # Must complete without raising
        await adapter._receive_loop(ws)


# ---------------------------------------------------------------------------
# Event Bus tick publication
# ---------------------------------------------------------------------------


class TestEventBusTickPublication:
    async def test_tick_published_to_event_bus(self) -> None:
        bus = _fake_event_bus()
        adapter = _make_adapter(event_bus=bus)
        adapter._running = True

        ws = AsyncMock()

        async def _msgs():  # type: ignore[return]
            yield json.dumps(_mini_ticker_frame("BTCUSDT"))

        ws.__aiter__ = lambda _: _msgs()
        await adapter._receive_loop(ws)

        # Give the created task a chance to run
        await asyncio.sleep(0)
        bus.publish_tick.assert_awaited()

    async def test_no_event_bus_does_not_raise(self) -> None:
        adapter = _make_adapter(event_bus=None)
        adapter._running = True

        ws = AsyncMock()

        async def _msgs():  # type: ignore[return]
            yield json.dumps(_mini_ticker_frame("ETHUSDT"))

        ws.__aiter__ = lambda _: _msgs()
        await adapter._receive_loop(ws)  # should not raise


# ---------------------------------------------------------------------------
# Disconnect lifecycle
# ---------------------------------------------------------------------------


class TestDisconnect:
    async def test_disconnect_sets_running_false(self) -> None:
        adapter = _make_adapter()
        adapter._running = True
        adapter._ws = None
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

    async def test_disconnect_cancels_connection_task(self) -> None:
        adapter = _make_adapter()

        async def _forever() -> None:
            await asyncio.sleep(3600)

        task = asyncio.create_task(_forever())
        adapter._connection_task = task
        adapter._running = True
        await adapter.disconnect()
        assert task.cancelled()

    async def test_disconnect_cancels_heartbeat_task(self) -> None:
        adapter = _make_adapter()

        async def _forever() -> None:
            await asyncio.sleep(3600)

        task = asyncio.create_task(_forever())
        adapter._heartbeat_task = task
        adapter._running = True
        await adapter.disconnect()
        assert task.cancelled()

    async def test_double_disconnect_is_safe(self) -> None:
        adapter = _make_adapter()
        await adapter.disconnect()
        await adapter.disconnect()  # should not raise


# ---------------------------------------------------------------------------
# Connect stores symbols
# ---------------------------------------------------------------------------


class TestConnect:
    async def test_connect_uppercases_symbols(self) -> None:
        adapter = _make_adapter()

        async def _noop() -> None:
            pass

        with pytest.MonkeyPatch().context() as mp:
            mp.setattr(adapter, "_run_connection_loop", _noop)
            await adapter.connect(symbols=["btcusdt", "ethusdt"])
        assert adapter._symbols == ["BTCUSDT", "ETHUSDT"]

    async def test_connect_sets_running_true(self) -> None:
        adapter = _make_adapter()

        async def _noop() -> None:
            pass

        with pytest.MonkeyPatch().context() as mp:
            mp.setattr(adapter, "_run_connection_loop", _noop)
            await adapter.connect(symbols=["BTCUSDT"])
        assert adapter._running is True
