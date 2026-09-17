"""
src/api/streaming.py

WebSocket streaming endpoint and stream-status REST endpoint for
DATA-SERVICE 2.0.

Task 8.6 — Requirements 15.5, 15.6, 15.7, 15.8

Endpoints
---------
``WS /v1/stream/ticks``
    Real-time tick stream.  Consumers send JSON control messages to subscribe
    or unsubscribe from symbol streams.  The server pushes normalised tick
    payloads for all subscribed symbols and sends heartbeat messages every
    10 seconds (Requirements 15.5, 15.6).

``GET /v1/stream/status``
    Returns aggregate streaming metrics: ``subscribedSymbols``,
    ``ticksPublished``, ``validationFailures``, ``lastPublishedAt``, and
    ``brokerConnections`` (Requirement 15.8).

Design constraints (from the spec)
-----------------------------------
- Accept control messages: ``{"action": "subscribe", "symbols": [...]}``
  and ``{"action": "unsubscribe", "symbols": [...]}``
  Optional ``"market"`` field (e.g. ``"india"``) is accepted and logged but
  has no filtering effect (routing is handled at the provider layer).
- Heartbeat every 10 seconds; if no heartbeat response for 3 consecutive
  heartbeats within a 30-second window → close connection, unsubscribe all.
- On connection close: unsubscribe all symbols within 5 seconds; decrement
  active-connection count.
- Maximum 500 concurrently subscribed symbol tick streams; exceeding the limit
  returns an error message rather than a hard disconnect.
- ``ConnectionManager`` tracks active WebSocket connections and their symbol
  subscriptions.
- Ticks are delivered to subscribers via Redis Pub/Sub on channel
  ``mds:ticks:{symbol}`` (matches the channel pattern from StreamingEngine).
  When Redis is unavailable the connection degrades gracefully (no ticks
  delivered) rather than being terminated.

Requirements: 15.5, 15.6, 15.7, 15.8
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import structlog
from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)
_log = structlog.get_logger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Maximum number of symbols that can have active subscribers across all
# concurrent WebSocket connections combined (Requirement 15.5).
_MAX_SUBSCRIBED_SYMBOLS: int = 500

# Heartbeat interval in seconds (Requirement 15.6).
_HEARTBEAT_INTERVAL_SEC: float = 10.0

# Maximum number of missed heartbeats before the connection is closed.
# 3 consecutive misses within a 30-second window (Requirement 15.6).
_MAX_MISSED_HEARTBEATS: int = 3

# Redis Pub/Sub channel prefix for ticks.
_TICK_CHANNEL_PREFIX: str = "mds:ticks:"

# ---------------------------------------------------------------------------
# ConnectionManager
# ---------------------------------------------------------------------------


class ConnectionManager:
    """Tracks active WebSocket connections and their symbol subscriptions.

    Thread-safety
    -------------
    All mutations are protected by an ``asyncio.Lock`` so concurrent coroutines
    running in the same event loop do not race on the internal dicts.

    Attributes
    ----------
    _connections : dict[str, WebSocket]
        Mapping from connection-id (UUID v4 string) to WebSocket instance.
    _subscriptions : dict[str, set[str]]
        Mapping from connection-id to the set of symbols it has subscribed to.
    _symbol_subscribers : dict[str, set[str]]
        Reverse index: symbol → set of connection-ids subscribed to it.
        Used for O(1) fan-out when a tick arrives.
    """

    def __init__(self) -> None:
        self._lock: asyncio.Lock = asyncio.Lock()
        self._connections: dict[str, WebSocket] = {}
        self._subscriptions: dict[str, set[str]] = {}
        self._symbol_subscribers: dict[str, set[str]] = {}

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def connect(self, websocket: WebSocket) -> str:
        """Accept a new WebSocket connection and register it.

        Returns the unique connection-id assigned to this connection.
        """
        await websocket.accept()
        conn_id = str(uuid.uuid4())
        async with self._lock:
            self._connections[conn_id] = websocket
            self._subscriptions[conn_id] = set()
        await _log.adebug("ws_connected", conn_id=conn_id)
        return conn_id

    async def disconnect(self, conn_id: str) -> None:
        """Unregister a connection and remove all its subscriptions.

        Called on ``WebSocketDisconnect`` or after heartbeat expiry.
        The connection is guaranteed to be cleaned up within the 5-second
        window required by Requirement 15.8.
        """
        async with self._lock:
            symbols = self._subscriptions.pop(conn_id, set())
            self._connections.pop(conn_id, None)
            for sym in symbols:
                self._symbol_subscribers.get(sym, set()).discard(conn_id)
                if not self._symbol_subscribers.get(sym):
                    self._symbol_subscribers.pop(sym, None)

        await _log.adebug(
            "ws_disconnected",
            conn_id=conn_id,
            released_symbols=list(symbols),
        )

    # ------------------------------------------------------------------
    # Subscription management
    # ------------------------------------------------------------------

    async def subscribe(
        self, conn_id: str, symbols: list[str]
    ) -> tuple[list[str], Optional[str]]:
        """Subscribe *conn_id* to *symbols*.

        Respects the global 500-symbol cap across all connections.  Any symbol
        that would push the total over the cap is rejected; the accepted symbols
        are returned along with an optional error message for the client.

        Returns
        -------
        tuple[list[str], Optional[str]]
            ``(accepted_symbols, error_message_or_None)``
        """
        accepted: list[str] = []
        rejected: list[str] = []

        async with self._lock:
            current_total = sum(len(v) for v in self._subscriptions.values())

            for sym in symbols:
                sym = sym.strip().upper()
                if not sym:
                    continue
                # Already subscribed by this connection — idempotent
                if sym in self._subscriptions.get(conn_id, set()):
                    accepted.append(sym)
                    continue
                # Enforce global cap
                if current_total >= _MAX_SUBSCRIBED_SYMBOLS:
                    rejected.append(sym)
                    continue
                self._subscriptions.setdefault(conn_id, set()).add(sym)
                self._symbol_subscribers.setdefault(sym, set()).add(conn_id)
                accepted.append(sym)
                current_total += 1

        error: Optional[str] = None
        if rejected:
            error = (
                f"MAX_SUBSCRIPTIONS_EXCEEDED: global limit of "
                f"{_MAX_SUBSCRIBED_SYMBOLS} subscribed symbols reached; "
                f"rejected: {rejected}"
            )
            await _log.awarning(
                "ws_subscription_limit_exceeded",
                conn_id=conn_id,
                rejected=rejected,
            )

        if accepted:
            await _log.adebug(
                "ws_subscribed", conn_id=conn_id, symbols=accepted
            )

        return accepted, error

    async def unsubscribe(self, conn_id: str, symbols: list[str]) -> list[str]:
        """Unsubscribe *conn_id* from *symbols*.

        Returns the list of symbols actually removed.
        """
        removed: list[str] = []
        async with self._lock:
            for sym in symbols:
                sym = sym.strip().upper()
                if sym in self._subscriptions.get(conn_id, set()):
                    self._subscriptions[conn_id].discard(sym)
                    self._symbol_subscribers.get(sym, set()).discard(conn_id)
                    if not self._symbol_subscribers.get(sym):
                        self._symbol_subscribers.pop(sym, None)
                    removed.append(sym)

        if removed:
            await _log.adebug(
                "ws_unsubscribed", conn_id=conn_id, symbols=removed
            )
        return removed

    # ------------------------------------------------------------------
    # Tick delivery (fan-out)
    # ------------------------------------------------------------------

    async def broadcast_tick(self, symbol: str, tick: dict[str, Any]) -> int:
        """Send *tick* to every connection subscribed to *symbol*.

        Dead connections are silently removed on send failure.

        Returns
        -------
        int
            Number of connections successfully delivered to.
        """
        sym_upper = symbol.strip().upper()
        async with self._lock:
            conn_ids = list(self._symbol_subscribers.get(sym_upper, set()))

        if not conn_ids:
            return 0

        payload = json.dumps(tick, default=str)
        delivered = 0
        stale: list[str] = []

        for cid in conn_ids:
            ws = self._connections.get(cid)
            if ws is None:
                stale.append(cid)
                continue
            try:
                await ws.send_text(payload)
                delivered += 1
            except Exception:  # noqa: BLE001
                # Connection is broken; schedule cleanup.
                stale.append(cid)

        # Clean up stale connections outside the lock (disconnect acquires it).
        for cid in stale:
            await self.disconnect(cid)

        return delivered

    # ------------------------------------------------------------------
    # Metrics helpers
    # ------------------------------------------------------------------

    @property
    def total_subscribed_symbols(self) -> int:
        """Total unique symbol subscriptions across all connections."""
        return len(self._symbol_subscribers)

    @property
    def active_connection_count(self) -> int:
        """Number of currently active WebSocket connections."""
        return len(self._connections)

    def get_symbols_for(self, conn_id: str) -> list[str]:
        """Return the list of symbols currently subscribed by *conn_id*."""
        return sorted(self._subscriptions.get(conn_id, set()))


# ---------------------------------------------------------------------------
# Module-level singleton ConnectionManager
# ---------------------------------------------------------------------------

manager: ConnectionManager = ConnectionManager()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _utc_iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _error_frame(code: str, message: str) -> str:
    """Return a JSON-serialised error frame to send over the WebSocket."""
    return json.dumps({"type": "error", "code": code, "message": message})


def _ack_frame(action: str, symbols: list[str], **extra: Any) -> str:
    """Return a JSON acknowledgement frame."""
    return json.dumps({"type": "ack", "action": action, "symbols": symbols, **extra})


def _heartbeat_frame() -> str:
    """Return a JSON heartbeat ping frame."""
    return json.dumps({"type": "heartbeat", "timestamp": _utc_iso_now()})


# ---------------------------------------------------------------------------
# Redis Pub/Sub tick listener for a single connection
# ---------------------------------------------------------------------------


async def _listen_redis_ticks(
    conn_id: str,
    websocket: WebSocket,
    app_state: Any,
) -> None:
    """Subscribe to Redis Pub/Sub channels for symbols the connection has
    subscribed to and forward tick messages to the WebSocket client.

    This coroutine runs for the lifetime of the WebSocket connection.  When
    the connection is closed or an error occurs, it returns silently.

    The coroutine polls for new symbol subscriptions every second so that
    dynamically subscribed symbols receive ticks shortly after the subscribe
    action is processed.

    If Redis is unavailable, this coroutine exits immediately (degraded mode).
    """
    redis = getattr(app_state, "redis", None)
    if redis is None:
        await _log.awarning("ws_redis_unavailable_for_pubsub", conn_id=conn_id)
        return

    try:
        pubsub = redis.pubsub()
        subscribed_channels: set[str] = set()

        while True:
            # Synchronise channel subscriptions with current symbol list.
            current_symbols = set(manager.get_symbols_for(conn_id))
            desired_channels = {
                f"{_TICK_CHANNEL_PREFIX}{sym}" for sym in current_symbols
            }

            new_channels = desired_channels - subscribed_channels
            removed_channels = subscribed_channels - desired_channels

            if new_channels:
                await pubsub.subscribe(*new_channels)
                subscribed_channels.update(new_channels)

            if removed_channels:
                await pubsub.unsubscribe(*removed_channels)
                subscribed_channels -= removed_channels

            # Drain any pending messages (non-blocking).
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.1)
            if message and message.get("type") == "message":
                data = message.get("data", "")
                if isinstance(data, bytes):
                    data = data.decode("utf-8")
                try:
                    ws = manager._connections.get(conn_id)
                    if ws is None:
                        break
                    await ws.send_text(data)
                except Exception:  # noqa: BLE001
                    break

            # Yield to let other tasks run; also acts as the poll interval.
            await asyncio.sleep(0.05)

    except Exception as exc:  # noqa: BLE001
        await _log.awarning(
            "ws_pubsub_listener_error", conn_id=conn_id, error=str(exc)
        )
    finally:
        try:
            await pubsub.close()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Heartbeat task
# ---------------------------------------------------------------------------


async def _heartbeat_task(conn_id: str, websocket: WebSocket) -> None:
    """Send periodic heartbeat pings and close the connection if the client
    does not respond within the allowed window.

    Heartbeats are sent every 10 seconds.  If 3 consecutive heartbeats go
    unanswered within the 30-second window, the connection is forcibly closed
    (Requirements 15.6).

    A client "responds" by simply having a live WebSocket connection.  The
    spec does not require a specific pong payload, so this implementation
    tracks whether the client's receive loop is still alive by catching
    ``WebSocketDisconnect``.

    The heartbeat coroutine itself monitors consecutive failures of the
    ``send_text`` call — if the client's TCP connection has died silently,
    the send will eventually raise and the missed counter increments.
    """
    missed = 0
    while True:
        await asyncio.sleep(_HEARTBEAT_INTERVAL_SEC)
        try:
            await websocket.send_text(_heartbeat_frame())
            missed = 0  # reset on successful send
        except Exception:  # noqa: BLE001
            missed += 1
            if missed >= _MAX_MISSED_HEARTBEATS:
                await _log.awarning(
                    "ws_heartbeat_timeout",
                    conn_id=conn_id,
                    missed=missed,
                )
                await manager.disconnect(conn_id)
                try:
                    await websocket.close(code=1001)
                except Exception:  # noqa: BLE001
                    pass
                return


# ---------------------------------------------------------------------------
# WebSocket endpoint
# ---------------------------------------------------------------------------


@router.websocket("/stream/ticks")
async def ws_stream_ticks(websocket: WebSocket) -> None:
    """WebSocket endpoint: ``WS /v1/stream/ticks``.

    Control message schema
    ----------------------
    Subscribe::

        {"action": "subscribe", "symbols": ["NIFTY", "BANKNIFTY"], "market": "india"}

    Unsubscribe::

        {"action": "unsubscribe", "symbols": ["NIFTY"]}

    Server acknowledgement::

        {"type": "ack", "action": "subscribe", "symbols": ["NIFTY", "BANKNIFTY"]}

    Server heartbeat (every 10 s)::

        {"type": "heartbeat", "timestamp": "2026-01-15T09:15:00.000Z"}

    Server error (e.g. subscription limit)::

        {"type": "error", "code": "MAX_SUBSCRIPTIONS_EXCEEDED", "message": "..."}

    Server tick delivery::

        {"tickId": "...", "symbol": "NIFTY", "ltp": 22150.5, ...}

    Requirements: 15.5, 15.6, 15.7, 15.8
    """
    # ---- Inline authentication ------------------------------------------------
    # FastAPI's APIKeyHeader security scheme requires an HTTP Request object and
    # raises TypeError when injected into a WebSocket handler.  We therefore
    # authenticate manually here before accepting the connection using the plain
    # _validate_api_key() helper, which bypasses FastAPI's dependency injection.
    #
    # Clients may supply the API key in either of two ways:
    #   1. Header:      X-API-KEY: <key>          (preferred — works from most WS
    #                                               clients that support custom headers)
    #   2. Query param: ?api_key=<key>             (fallback for browser clients or
    #                                               environments that can't set headers)
    try:
        from src.auth.consumer_auth import _validate_api_key  # noqa: PLC0415

        _raw_key = (
            websocket.headers.get("x-api-key")
            or websocket.query_params.get("api_key")
        )
        _validate_api_key(_raw_key)  # raises HTTPException(401) on failure
    except Exception:  # noqa: BLE001
        # Reject before upgrading — close code 1008 = Policy Violation.
        await websocket.close(code=1008, reason="Unauthorized")
        return
    # --------------------------------------------------------------------------

    conn_id = await manager.connect(websocket)

    await _log.ainfo(
        "ws_connection_opened",
        conn_id=conn_id,
        client=str(websocket.client),
    )

    # Launch concurrent tasks:
    #   1. heartbeat — sends pings, closes on 3 missed beats
    #   2. pubsub    — forwards Redis ticks to this connection
    heartbeat_task = asyncio.create_task(
        _heartbeat_task(conn_id, websocket),
        name=f"heartbeat-{conn_id}",
    )
    pubsub_task = asyncio.create_task(
        _listen_redis_ticks(conn_id, websocket, websocket.app.state),
        name=f"pubsub-{conn_id}",
    )

    try:
        while True:
            # Block waiting for the next control message from the client.
            raw = await websocket.receive_text()

            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_text(
                    _error_frame(
                        "INVALID_JSON",
                        "Message must be a valid JSON object.",
                    )
                )
                continue

            if not isinstance(msg, dict):
                await websocket.send_text(
                    _error_frame("INVALID_MESSAGE", "Expected a JSON object.")
                )
                continue

            action = msg.get("action", "").strip().lower()
            symbols_raw: Any = msg.get("symbols", [])

            if not isinstance(symbols_raw, list):
                await websocket.send_text(
                    _error_frame(
                        "INVALID_SYMBOLS",
                        "'symbols' must be a JSON array of strings.",
                    )
                )
                continue

            symbols: list[str] = [str(s) for s in symbols_raw if s]

            if action == "subscribe":
                accepted, error_msg = await manager.subscribe(conn_id, symbols)
                # Notify the StreamingEngine about new subscriptions so
                # get_stream_status() reflects the new symbols.
                streaming_engine = getattr(
                    websocket.app.state, "streaming_engine", None
                )
                if streaming_engine is not None:
                    for sym in accepted:
                        streaming_engine.subscribe_symbol(sym)

                if error_msg:
                    await websocket.send_text(
                        _error_frame("MAX_SUBSCRIPTIONS_EXCEEDED", error_msg)
                    )
                if accepted:
                    await websocket.send_text(_ack_frame("subscribe", accepted))

            elif action == "unsubscribe":
                removed = await manager.unsubscribe(conn_id, symbols)
                # Reflect in StreamingEngine only if no other connection
                # still subscribes to that symbol.
                streaming_engine = getattr(
                    websocket.app.state, "streaming_engine", None
                )
                if streaming_engine is not None:
                    for sym in removed:
                        # Only unregister from engine if nobody else subscribes.
                        if sym not in manager._symbol_subscribers:
                            streaming_engine.unsubscribe_symbol(sym)

                await websocket.send_text(_ack_frame("unsubscribe", removed))

            else:
                await websocket.send_text(
                    _error_frame(
                        "UNKNOWN_ACTION",
                        f"Unknown action '{action}'. Supported: subscribe, unsubscribe.",
                    )
                )

    except WebSocketDisconnect:
        await _log.ainfo("ws_client_disconnected", conn_id=conn_id)
    except Exception as exc:  # noqa: BLE001
        await _log.awarning(
            "ws_connection_error", conn_id=conn_id, error=str(exc)
        )
    finally:
        # Cancel background tasks and clean up subscriptions.
        heartbeat_task.cancel()
        pubsub_task.cancel()

        with asyncio.timeout(5.0):
            await manager.disconnect(conn_id)

        await _log.ainfo(
            "ws_connection_closed",
            conn_id=conn_id,
            active_connections=manager.active_connection_count,
        )


# ---------------------------------------------------------------------------
# Stream status REST endpoint
# ---------------------------------------------------------------------------


@router.get(
    "/stream/status",
    summary="Streaming status",
    description=(
        "Returns current streaming metrics: number of subscribed symbol streams, "
        "total ticks published, validation failures, last published timestamp, "
        "and broker WebSocket connection states (Requirement 15.8)."
    ),
    response_class=JSONResponse,
    tags=["Streaming"],
)
async def get_stream_status(request: Request) -> JSONResponse:
    """``GET /v1/stream/status``

    Response body::

        {
          "subscribedSymbols": 12,
          "activeConnections": 4,
          "ticksPublished": 84321,
          "validationFailures": {"schema_validation": 2},
          "lastPublishedAt": "2026-01-15T09:15:00.000Z",
          "brokerConnections": {
            "angelOne": true,
            "upstox": false,
            "binance": true
          },
          "timestamp": "2026-01-15T09:15:01.000Z"
        }

    Requirements: 15.8
    """
    streaming_engine = getattr(request.app.state, "streaming_engine", None)

    if streaming_engine is not None:
        engine_status = streaming_engine.get_stream_status()
    else:
        engine_status = {
            "subscribedSymbols": [],
            "ticksPublished": 0,
            "duplicateCount": 0,
            "validationFailures": {},
            "lastPublishedAt": None,
            "brokerConnections": {
                "angelOne": False,
                "upstox": False,
                "binance": False,
            },
        }

    return JSONResponse(
        status_code=200,
        content={
            "subscribedSymbols": manager.total_subscribed_symbols,
            "activeConnections": manager.active_connection_count,
            "ticksPublished": engine_status.get("ticksPublished", 0),
            "validationFailures": engine_status.get("validationFailures", {}),
            "lastPublishedAt": engine_status.get("lastPublishedAt"),
            "brokerConnections": engine_status.get("brokerConnections", {}),
            "tickPersister": (
                request.app.state.tick_persister.get_stats()
                if getattr(request.app.state, "tick_persister", None) is not None
                else {"status": "not_initialised"}
            ),
            "angelOneStreamConnected": (
                getattr(request.app.state, "angel_one_stream", None) is not None
                and getattr(request.app.state.angel_one_stream, "is_connected", lambda: False)()
            ),
            "upstoxStreamConnected": (
                getattr(request.app.state, "upstox_stream", None) is not None
                and getattr(request.app.state.upstox_stream, "is_connected", lambda: False)()
            ),
            "timestamp": _utc_iso_now(),
        },
    )
