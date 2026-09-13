"""
Unit tests for WebSocket streaming endpoint and ConnectionManager (Task 8.6).

Requirements: 15.5, 15.6, 15.7, 15.8

Tests use an in-process ASGI test client (``httpx.AsyncClient`` via
``ASGITransport``) for the REST endpoint and ``httpx``'s ``aconnect_ws``
(via ``starlette.testclient.TestClient`` WebSocket helper) for WebSocket
interactions.  No live Redis or PostgreSQL is required.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.api.streaming import (
    ConnectionManager,
    _MAX_SUBSCRIBED_SYMBOLS,
    manager,
    router,
)


# ---------------------------------------------------------------------------
# Minimal FastAPI test app
# ---------------------------------------------------------------------------


def _make_app(streaming_engine: Any = None) -> FastAPI:
    """Build a minimal FastAPI app that includes the streaming router."""
    app = FastAPI()
    app.include_router(router, prefix="/v1")
    app.state.streaming_engine = streaming_engine
    app.state.redis = None  # Redis not required for unit tests
    return app


# ---------------------------------------------------------------------------
# ConnectionManager unit tests
# ---------------------------------------------------------------------------


class TestConnectionManager:
    """Unit tests for ConnectionManager without a live WebSocket."""

    @pytest.mark.asyncio
    async def test_connect_assigns_unique_ids(self) -> None:
        """Each connect() call should return a distinct connection-id."""
        mgr = ConnectionManager()
        ws1 = AsyncMock()
        ws2 = AsyncMock()

        id1 = await mgr.connect(ws1)
        id2 = await mgr.connect(ws2)

        assert id1 != id2
        assert mgr.active_connection_count == 2

    @pytest.mark.asyncio
    async def test_connect_calls_websocket_accept(self) -> None:
        """connect() must call websocket.accept() so the handshake completes."""
        mgr = ConnectionManager()
        ws = AsyncMock()
        await mgr.connect(ws)
        ws.accept.assert_called_once()

    @pytest.mark.asyncio
    async def test_disconnect_removes_connection(self) -> None:
        mgr = ConnectionManager()
        ws = AsyncMock()
        conn_id = await mgr.connect(ws)
        assert mgr.active_connection_count == 1

        await mgr.disconnect(conn_id)
        assert mgr.active_connection_count == 0

    @pytest.mark.asyncio
    async def test_disconnect_removes_symbol_subscriptions(self) -> None:
        mgr = ConnectionManager()
        ws = AsyncMock()
        conn_id = await mgr.connect(ws)
        await mgr.subscribe(conn_id, ["NIFTY", "BANKNIFTY"])

        await mgr.disconnect(conn_id)

        assert mgr.total_subscribed_symbols == 0
        assert mgr.get_symbols_for(conn_id) == []

    @pytest.mark.asyncio
    async def test_subscribe_adds_symbols(self) -> None:
        mgr = ConnectionManager()
        ws = AsyncMock()
        conn_id = await mgr.connect(ws)

        accepted, error = await mgr.subscribe(conn_id, ["NIFTY", "BANKNIFTY"])

        assert sorted(accepted) == ["BANKNIFTY", "NIFTY"]
        assert error is None
        assert sorted(mgr.get_symbols_for(conn_id)) == ["BANKNIFTY", "NIFTY"]
        assert mgr.total_subscribed_symbols == 2

    @pytest.mark.asyncio
    async def test_subscribe_normalises_to_uppercase(self) -> None:
        mgr = ConnectionManager()
        ws = AsyncMock()
        conn_id = await mgr.connect(ws)

        accepted, _ = await mgr.subscribe(conn_id, ["nifty", "Banknifty"])
        assert "NIFTY" in accepted
        assert "BANKNIFTY" in accepted

    @pytest.mark.asyncio
    async def test_subscribe_is_idempotent(self) -> None:
        """Subscribing the same symbol twice should not double-count it."""
        mgr = ConnectionManager()
        ws = AsyncMock()
        conn_id = await mgr.connect(ws)

        await mgr.subscribe(conn_id, ["NIFTY"])
        accepted, error = await mgr.subscribe(conn_id, ["NIFTY"])

        assert "NIFTY" in accepted
        assert error is None
        assert mgr.total_subscribed_symbols == 1

    @pytest.mark.asyncio
    async def test_unsubscribe_removes_symbols(self) -> None:
        mgr = ConnectionManager()
        ws = AsyncMock()
        conn_id = await mgr.connect(ws)
        await mgr.subscribe(conn_id, ["NIFTY", "BANKNIFTY"])

        removed = await mgr.unsubscribe(conn_id, ["NIFTY"])

        assert removed == ["NIFTY"]
        assert mgr.get_symbols_for(conn_id) == ["BANKNIFTY"]
        assert mgr.total_subscribed_symbols == 1

    @pytest.mark.asyncio
    async def test_unsubscribe_nonexistent_symbol_is_safe(self) -> None:
        mgr = ConnectionManager()
        ws = AsyncMock()
        conn_id = await mgr.connect(ws)

        removed = await mgr.unsubscribe(conn_id, ["GHOST"])
        assert removed == []

    @pytest.mark.asyncio
    async def test_subscribe_respects_global_cap(self) -> None:
        """When the global cap is reached, additional symbols must be rejected."""
        mgr = ConnectionManager()
        # Fill up to the cap using a single fake connection.
        ws = AsyncMock()
        conn_id = await mgr.connect(ws)

        # Build a list of unique symbols equal to the cap.
        all_syms = [f"SYM{i}" for i in range(_MAX_SUBSCRIBED_SYMBOLS)]
        accepted, error = await mgr.subscribe(conn_id, all_syms)
        assert len(accepted) == _MAX_SUBSCRIBED_SYMBOLS
        assert error is None

        # Any further symbol should be rejected.
        ws2 = AsyncMock()
        conn_id2 = await mgr.connect(ws2)
        accepted2, error2 = await mgr.subscribe(conn_id2, ["EXTRA"])
        assert accepted2 == []
        assert error2 is not None
        assert "MAX_SUBSCRIPTIONS_EXCEEDED" in error2

    @pytest.mark.asyncio
    async def test_broadcast_tick_delivers_to_subscriber(self) -> None:
        mgr = ConnectionManager()
        ws = AsyncMock()
        conn_id = await mgr.connect(ws)
        await mgr.subscribe(conn_id, ["NIFTY"])

        tick = {"symbol": "NIFTY", "ltp": 22100.0}
        delivered = await mgr.broadcast_tick("NIFTY", tick)

        assert delivered == 1
        ws.send_text.assert_called_once()
        sent_payload = json.loads(ws.send_text.call_args[0][0])
        assert sent_payload["ltp"] == 22100.0

    @pytest.mark.asyncio
    async def test_broadcast_tick_does_not_deliver_to_non_subscriber(self) -> None:
        mgr = ConnectionManager()
        ws = AsyncMock()
        conn_id = await mgr.connect(ws)
        await mgr.subscribe(conn_id, ["BANKNIFTY"])

        delivered = await mgr.broadcast_tick("NIFTY", {"symbol": "NIFTY", "ltp": 1.0})
        assert delivered == 0
        ws.send_text.assert_not_called()

    @pytest.mark.asyncio
    async def test_broadcast_tick_cleans_stale_connections(self) -> None:
        """broadcast_tick should remove broken connections silently."""
        mgr = ConnectionManager()
        ws = AsyncMock()
        ws.send_text.side_effect = Exception("broken pipe")
        conn_id = await mgr.connect(ws)
        await mgr.subscribe(conn_id, ["NIFTY"])

        delivered = await mgr.broadcast_tick("NIFTY", {"symbol": "NIFTY"})

        assert delivered == 0
        # Stale connection cleaned up
        assert mgr.active_connection_count == 0

    @pytest.mark.asyncio
    async def test_broadcast_tick_case_insensitive_channel(self) -> None:
        """broadcast_tick should match symbol regardless of case."""
        mgr = ConnectionManager()
        ws = AsyncMock()
        conn_id = await mgr.connect(ws)
        await mgr.subscribe(conn_id, ["nifty"])  # subscribed in lowercase

        delivered = await mgr.broadcast_tick("NIFTY", {"symbol": "NIFTY", "ltp": 1.0})
        assert delivered == 1

    @pytest.mark.asyncio
    async def test_multiple_subscribers_to_same_symbol(self) -> None:
        """All subscribers of a symbol must receive the broadcast."""
        mgr = ConnectionManager()
        ws_a, ws_b = AsyncMock(), AsyncMock()
        id_a = await mgr.connect(ws_a)
        id_b = await mgr.connect(ws_b)
        await mgr.subscribe(id_a, ["NIFTY"])
        await mgr.subscribe(id_b, ["NIFTY"])

        delivered = await mgr.broadcast_tick("NIFTY", {"symbol": "NIFTY", "ltp": 22000.0})
        assert delivered == 2
        ws_a.send_text.assert_called_once()
        ws_b.send_text.assert_called_once()


# ---------------------------------------------------------------------------
# REST endpoint: GET /v1/stream/status
# ---------------------------------------------------------------------------


class TestStreamStatusEndpoint:
    """Tests for GET /v1/stream/status."""

    @pytest.mark.asyncio
    async def test_status_returns_200(self) -> None:
        app = _make_app()
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/v1/stream/status")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_status_shape_without_streaming_engine(self) -> None:
        """When no streaming engine is attached, defaults are used."""
        app = _make_app(streaming_engine=None)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/v1/stream/status")

        body = resp.json()
        assert "subscribedSymbols" in body
        assert "activeConnections" in body
        assert "ticksPublished" in body
        assert "validationFailures" in body
        assert "lastPublishedAt" in body
        assert "brokerConnections" in body
        assert "timestamp" in body

    @pytest.mark.asyncio
    async def test_status_reflects_streaming_engine(self) -> None:
        """Status endpoint should forward StreamingEngine metrics."""
        engine = MagicMock()
        engine.get_stream_status.return_value = {
            "subscribedSymbols": ["NIFTY"],
            "ticksPublished": 9876,
            "duplicateCount": 3,
            "validationFailures": {"schema_validation": 1},
            "lastPublishedAt": "2026-01-15T09:15:00.000Z",
            "brokerConnections": {
                "angelOne": True,
                "upstox": False,
                "binance": True,
            },
        }
        app = _make_app(streaming_engine=engine)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/v1/stream/status")

        body = resp.json()
        assert body["ticksPublished"] == 9876
        assert body["brokerConnections"]["angelOne"] is True
        assert body["brokerConnections"]["upstox"] is False
        assert body["validationFailures"] == {"schema_validation": 1}

    @pytest.mark.asyncio
    async def test_status_timestamp_is_utc_iso8601(self) -> None:
        app = _make_app()
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/v1/stream/status")

        ts = resp.json()["timestamp"]
        # Must end with Z (UTC) and be ISO-8601 shaped.
        assert ts.endswith("Z")
        assert "T" in ts


# ---------------------------------------------------------------------------
# Helper / frame utility tests
# ---------------------------------------------------------------------------


class TestFrameHelpers:
    """Test that the internal frame helpers produce valid JSON."""

    def test_error_frame_is_valid_json(self) -> None:
        from src.api.streaming import _error_frame

        frame = _error_frame("TEST_CODE", "test message")
        parsed = json.loads(frame)
        assert parsed["type"] == "error"
        assert parsed["code"] == "TEST_CODE"
        assert parsed["message"] == "test message"

    def test_ack_frame_is_valid_json(self) -> None:
        from src.api.streaming import _ack_frame

        frame = _ack_frame("subscribe", ["NIFTY", "BANKNIFTY"])
        parsed = json.loads(frame)
        assert parsed["type"] == "ack"
        assert parsed["action"] == "subscribe"
        assert "NIFTY" in parsed["symbols"]

    def test_heartbeat_frame_is_valid_json(self) -> None:
        from src.api.streaming import _heartbeat_frame

        frame = _heartbeat_frame()
        parsed = json.loads(frame)
        assert parsed["type"] == "heartbeat"
        assert "timestamp" in parsed
        assert parsed["timestamp"].endswith("Z")
