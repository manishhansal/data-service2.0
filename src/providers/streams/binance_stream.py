"""
src/providers/streams/binance_stream.py

Binance WebSocket adapter for DATA-SERVICE 2.0.

Maintains persistent Binance combined-stream mini-ticker WebSocket
connections for live crypto spot / futures data.

Key behaviours (Requirements 13.5, 13.6, 13.11)
-------------------------------------------------
- Combined stream URL for all symbols in a single connection.
- Exponential backoff: base 1 s, max **30 s** (shorter ceiling than Indian
  providers).
- Heartbeat pings every **30 seconds**; Binance expects pongs or it drops
  the connection.
- On ``error`` / ``closed``: log ``symbol``, ``reason``,
  ``reconnectAttempt``; do NOT expose the raw WebSocket error to consumers.
- When established / re-established: publish received mini-ticker ticks
  to the Event Bus within 2 seconds of connection confirmation.

Mini-ticker stream frame format
---------------------------------
Binance combined-stream wraps each event as::

    {"stream": "btcusdt@miniTicker", "data": {...}}

The ``data`` object contains:
  - ``e``: event type (``"24hrMiniTicker"``)
  - ``E``: event time (UTC ms)
  - ``s``: symbol (e.g. ``"BTCUSDT"``)
  - ``c``: close price (string)
  - ``o``: open price (string)
  - ``h``: high price (string)
  - ``l``: low price (string)
  - ``v``: base-asset volume (string)
  - ``q``: quote-asset volume (string)

Requirements: 13.5, 13.6, 13.11
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PROVIDER_ID = "binance"

# Reconnect policy (shorter cap than Indian providers per design)
_BACKOFF_BASE_SEC: float = 1.0
_BACKOFF_MAX_SEC: float = 30.0  # Binance: capped at 30s, not 60s

# Heartbeat interval (Binance drops idle connections after ~10 min)
_HEARTBEAT_INTERVAL_SEC: float = 30.0

# Binance combined stream base URL
_WS_BASE_URL = "wss://stream.binance.com:9443/stream"

# Number of symbols after which a combined stream splits (Binance limit 1024)
_MAX_STREAMS_PER_CONNECTION = 1024


# ---------------------------------------------------------------------------
# Normalisation helper
# ---------------------------------------------------------------------------


def _normalise_mini_ticker(data: dict[str, Any]) -> dict[str, Any]:
    """Convert a Binance mini-ticker ``data`` payload to canonical schema.

    Args:
        data: The ``"data"`` sub-object from a Binance combined-stream frame.

    Returns:
        Canonical tick dict.
    """
    symbol = str(data.get("s", "")).upper()
    return {
        "tickId": str(uuid.uuid4()),
        "source": _PROVIDER_ID,
        "symbol": symbol,
        # Binance crypto ticks never have Indian-market OI; set null
        "oi": None,
        "oiMissing": True,
        "ltp": float(data.get("c", 0)),
        "open": float(data.get("o", 0)),
        "high": float(data.get("h", 0)),
        "low": float(data.get("l", 0)),
        "volume": float(data.get("v", 0)),
        "quoteVolume": float(data.get("q", 0)),
        "eventTimeMs": int(data.get("E", int(time.time() * 1000))),
        "receivedAtMs": int(time.time() * 1000),
        "isDuplicate": False,
        "exchange": "BINANCE",
    }


# ---------------------------------------------------------------------------
# BinanceStreamAdapter
# ---------------------------------------------------------------------------


class BinanceStreamAdapter:
    """Async adapter for the Binance combined-stream mini-ticker WebSocket.

    Manages a combined-stream connection that subscribes to ``@miniTicker``
    for each requested symbol.  Ticks are normalised and forwarded to
    ``on_tick_callback`` and, when an Event Bus is supplied, also published
    to ``mds:events:ticks``.

    Reconnect policy differs from Indian providers:
    - Base backoff: 1 s
    - Max backoff: **30 s** (not 60 s)
    - Heartbeat pings every **30 seconds**

    Parameters
    ----------
    event_bus:
        Optional ``EventBus`` instance for tick and connection-event
        publication.

    Usage
    -----
    ::

        adapter = BinanceStreamAdapter(event_bus=bus)
        adapter.on_tick_callback = my_handler

        await adapter.connect(symbols=["BTCUSDT", "ETHUSDT", "SOLUSDT"])

        # Later…
        await adapter.disconnect()
    """

    def __init__(self, event_bus: Any = None) -> None:
        self._event_bus = event_bus
        self._ws: Any = None
        self._connection_task: Optional[asyncio.Task[None]] = None
        self._heartbeat_task: Optional[asyncio.Task[None]] = None
        self._symbols: list[str] = []
        self._running: bool = False
        self._reconnect_attempts: int = 0
        self.on_tick_callback: Optional[Callable[[dict[str, Any]], None]] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def connect(self, symbols: list[str]) -> None:
        """Subscribe to mini-ticker streams for all *symbols*.

        Builds the combined stream URL and starts the background
        connection-management task.

        Args:
            symbols: Upper-case Binance symbol strings, e.g.
                     ``["BTCUSDT", "ETHUSDT", "SOLUSDT"]``.
        """
        self._symbols = [s.upper() for s in symbols]
        self._running = True
        self._reconnect_attempts = 0
        self._connection_task = asyncio.create_task(
            self._run_connection_loop(), name="binance_stream_loop"
        )
        logger.info(
            "binance_stream_connecting",
            extra={"provider": _PROVIDER_ID, "symbol_count": len(self._symbols)},
        )

    async def disconnect(self) -> None:
        """Close the WebSocket connection and cancel background tasks."""
        self._running = False
        for task in (self._heartbeat_task, self._connection_task):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._heartbeat_task = None
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        logger.info(
            "binance_stream_disconnected",
            extra={"provider": _PROVIDER_ID},
        )

    @staticmethod
    def _build_stream_url(symbols: list[str]) -> str:
        """Build the Binance combined-stream URL for mini-ticker subscriptions.

        Joins each symbol (lower-cased) with ``@miniTicker`` and combines
        them into a single ``/stream?streams=`` URL.

        Args:
            symbols: List of upper-case Binance symbol strings.

        Returns:
            A URL string such as::

                wss://stream.binance.com:9443/stream?streams=btcusdt@miniTicker/ethusdt@miniTicker

        Examples:
            >>> BinanceStreamAdapter._build_stream_url(["BTCUSDT", "ETHUSDT"])
            'wss://stream.binance.com:9443/stream?streams=btcusdt@miniTicker/ethusdt@miniTicker'
        """
        stream_names = "/".join(f"{s.lower()}@miniTicker" for s in symbols)
        return f"{_WS_BASE_URL}?streams={stream_names}"

    # ------------------------------------------------------------------
    # Internal connection loop
    # ------------------------------------------------------------------

    async def _run_connection_loop(self) -> None:
        """Background task: connect, receive, reconnect on failure."""
        while self._running:
            try:
                await self._connect_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not self._running:
                    break
                self._reconnect_attempts += 1
                backoff = min(
                    _BACKOFF_BASE_SEC * (2 ** (self._reconnect_attempts - 1)),
                    _BACKOFF_MAX_SEC,
                )
                # Log symbol + reason + attempt; do NOT expose raw WS error to consumers
                logger.warning(
                    "binance_stream_reconnecting",
                    extra={
                        "provider": _PROVIDER_ID,
                        "symbols": self._symbols,
                        "reason": type(exc).__name__,
                        "reconnectAttempt": self._reconnect_attempts,
                        "backoff_sec": backoff,
                    },
                )
                await asyncio.sleep(backoff)

    async def _connect_once(self) -> None:
        """Open WebSocket, start heartbeat task, and pump messages."""
        import websockets  # local import — optional dep in test environments

        url = self._build_stream_url(self._symbols)
        async with websockets.connect(url, ping_interval=None) as ws:
            self._ws = ws
            self._reconnect_attempts = 0
            logger.info(
                "binance_stream_connected",
                extra={
                    "provider": _PROVIDER_ID,
                    "symbol_count": len(self._symbols),
                },
            )
            # Start the heartbeat in a sub-task so _receive_loop stays clean
            self._heartbeat_task = asyncio.create_task(
                self._heartbeat_loop(ws), name="binance_heartbeat"
            )
            try:
                await self._receive_loop(ws)
            finally:
                if self._heartbeat_task and not self._heartbeat_task.done():
                    self._heartbeat_task.cancel()
                    try:
                        await self._heartbeat_task
                    except asyncio.CancelledError:
                        pass

    async def _receive_loop(self, ws: Any) -> None:
        """Pump incoming messages and route to the tick handler."""
        async for raw_message in ws:
            if not self._running:
                break
            try:
                frame = json.loads(
                    raw_message if isinstance(raw_message, str)
                    else raw_message.decode("utf-8")
                )
                # Combined stream wraps each event: {"stream": "...", "data": {...}}
                data = frame.get("data", frame)
                event_type = data.get("e", "")
                if event_type != "24hrMiniTicker":
                    # Heartbeat echo or other control frame — ignore silently
                    continue
                tick = _normalise_mini_ticker(data)
                if self.on_tick_callback is not None:
                    self.on_tick_callback(tick)
                if self._event_bus is not None:
                    symbol = tick.get("symbol", "")
                    asyncio.create_task(
                        self._event_bus.publish_tick(symbol, tick),
                        name=f"binance_tick_{symbol}",
                    )
            except Exception as exc:
                # Log at warning; do NOT re-raise or expose to consumer
                logger.warning(
                    "binance_tick_parse_error",
                    extra={
                        "provider": _PROVIDER_ID,
                        "reason": type(exc).__name__,
                        "reconnectAttempt": self._reconnect_attempts,
                    },
                )

    async def _heartbeat_loop(self, ws: Any) -> None:
        """Send periodic pings to keep the Binance connection alive.

        Binance expects regular activity on the WebSocket; without pings
        it may close the connection after ~10 minutes.
        """
        while self._running:
            await asyncio.sleep(_HEARTBEAT_INTERVAL_SEC)
            if not self._running:
                break
            try:
                await ws.ping()
                logger.debug(
                    "binance_heartbeat_sent",
                    extra={"provider": _PROVIDER_ID},
                )
            except Exception as exc:
                # Heartbeat failure will be caught by the receive loop
                logger.warning(
                    "binance_heartbeat_failed",
                    extra={"provider": _PROVIDER_ID, "reason": type(exc).__name__},
                )
                break

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        """True if the WebSocket is currently open."""
        return self._ws is not None and not getattr(self._ws, "closed", True)

    @property
    def reconnect_attempts(self) -> int:
        """Current consecutive reconnect attempt count."""
        return self._reconnect_attempts
