"""
Delta Exchange WebSocket adapter — DS2-RCA-001 fix.

Maintains a persistent Delta Exchange public WebSocket connection for live
ticker data (mark price, close price, volume).

Delta WebSocket protocol
------------------------
Connection URL (India):  ``wss://socket.india.delta.exchange``
Subscribe message::

    {
        "type": "subscribe",
        "payload": {
            "channels": [{"name": "ticker", "symbols": [...]}]
        }
    }

Delta supports two ticker payload shapes:

*Compact* (newer format)::

    {
        "type": "ticker",
        "sy": "BTCUSD",           # symbol
        "sp": "43210.50",         # price (string)
        "ts": 1700000000000000,   # timestamp (microseconds)
        "d": [{"ohlc": [open, high, low, close], "to": [turnover, turnover_usd]}]
    }

*Legacy* (``v2/ticker``)::

    {
        "type": "v2/ticker",
        "symbol": "BTCUSD",
        "close": "43210.50",
        "open": "43100.00",
        "high": "43500.00",
        "low": "43050.00",
        "mark_price": "43215.00",
        "spot_price": "43200.00",
        "turnover_usd": "1234567.89",
        "volume": "28.5",
        "timestamp": 1700000000000000
    }

Both shapes are normalised into the same canonical tick dict.

Reconnect policy
----------------
* Base backoff: 1 s  (same as BinanceStreamAdapter)
* Max backoff:  30 s
* Heartbeat ping every 25 s (Delta may close idle connections)

Requirements: DS2-RCA-001
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

_PROVIDER_ID = "delta"
_EXCHANGE = "DELTA"

_BACKOFF_BASE_SEC: float = 1.0
_BACKOFF_MAX_SEC: float = 30.0
_HEARTBEAT_INTERVAL_SEC: float = 25.0

_DEFAULT_WS_URL = "wss://socket.india.delta.exchange"


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------

def _micro_to_ms(value: int | float | None) -> int:
    """Convert Delta timestamp (µs, ms, or s) to UTC epoch ms."""
    if not value:
        return int(time.time() * 1000)
    if value > 1e14:   # microseconds
        return int(value / 1_000)
    if value > 1e11:   # milliseconds
        return int(value)
    return int(value * 1_000)   # seconds


def _normalise_compact_tick(data: dict[str, Any]) -> dict[str, Any] | None:
    """Parse Delta's compact ticker frame.

    Shape::
        {"type":"ticker", "sy": str, "sp": str, "ts": int,
         "d": [{"ohlc": [o,h,l,c], "to": [turnover, turnover_usd]}]}
    """
    symbol = str(data.get("sy", "")).upper()
    if not symbol:
        return None

    d_arr = data.get("d") or []
    ohlc = d_arr[0].get("ohlc", [0, 0, 0, 0]) if d_arr else [0, 0, 0, 0]
    to_arr = d_arr[0].get("to", [0, 0]) if d_arr else [0, 0]

    open_, high, low, close = (float(v) for v in (ohlc + [0, 0, 0, 0])[:4])
    turnover_usd = float((to_arr + [0, 0])[1])

    return {
        "tickId":        str(uuid.uuid4()),
        "source":        _PROVIDER_ID,
        "symbol":        symbol,
        "oi":            None,
        "oiMissing":     True,
        "ltp":           float(data.get("sp", close) or close),
        "open":          open_,
        "high":          high,
        "low":           low,
        "volume":        0.0,       # Not in compact shape
        "quoteVolume":   turnover_usd,
        "eventTimeMs":   _micro_to_ms(data.get("ts")),
        "receivedAtMs":  int(time.time() * 1000),
        "isDuplicate":   False,
        "exchange":      _EXCHANGE,
    }


def _normalise_legacy_tick(data: dict[str, Any]) -> dict[str, Any] | None:
    """Parse Delta's legacy v2/ticker frame."""
    symbol = str(data.get("symbol", "")).upper()
    if not symbol:
        return None

    close = float(data.get("close") or data.get("mark_price") or 0)
    return {
        "tickId":        str(uuid.uuid4()),
        "source":        _PROVIDER_ID,
        "symbol":        symbol,
        "oi":            None,
        "oiMissing":     True,
        "ltp":           close,
        "open":          float(data.get("open") or close),
        "high":          float(data.get("high") or close),
        "low":           float(data.get("low") or close),
        "volume":        float(data.get("volume") or 0),
        "quoteVolume":   float(data.get("turnover_usd") or 0),
        "eventTimeMs":   _micro_to_ms(data.get("timestamp")),
        "receivedAtMs":  int(time.time() * 1000),
        "isDuplicate":   False,
        "exchange":      _EXCHANGE,
    }


def _parse_tick(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Dispatch to the correct parser based on the ``type`` field."""
    msg_type = str(raw.get("type", ""))
    if msg_type == "ticker":
        return _normalise_compact_tick(raw)
    if msg_type == "v2/ticker":
        return _normalise_legacy_tick(raw)
    return None


# ---------------------------------------------------------------------------
# DeltaStreamAdapter
# ---------------------------------------------------------------------------


class DeltaStreamAdapter:
    """Async adapter for the Delta Exchange public ticker WebSocket.

    Manages a single WebSocket connection that subscribes to the ``ticker``
    channel for all requested symbols.  Ticks are normalised and forwarded
    to the ``on_tick_callback``.

    Parameters
    ----------
    symbols:
        List of Delta instrument symbols to subscribe to,
        e.g. ``["BTCUSD", "ETHUSD", "SOLUSD"]``.
    on_tick_callback:
        Async or sync callable invoked with each normalised tick dict.
    on_status_change:
        Optional callable invoked with a string status whenever the
        connection state changes (``"connecting"``, ``"open"``,
        ``"reconnecting"``, ``"closed"``).
    ws_url:
        Delta WebSocket base URL.  Defaults to the India endpoint.
    event_bus:
        Optional ``EventBus`` instance for publishing ticks to Redis Streams.
    """

    def __init__(
        self,
        symbols: list[str],
        on_tick_callback: Callable[[dict[str, Any]], Any],
        *,
        on_status_change: Optional[Callable[[str], Any]] = None,
        ws_url: str = _DEFAULT_WS_URL,
        event_bus: Any = None,
    ) -> None:
        self._symbols = [s.upper() for s in symbols]
        self._on_tick = on_tick_callback
        self._on_status_change = on_status_change
        self._ws_url = ws_url
        self._event_bus = event_bus

        self._running = False
        self._ws: Any = None  # websockets.ClientConnection when connected
        self._reconnect_attempts = 0
        self._task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------ #
    # Public lifecycle
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        """Start the WebSocket listener in a background task."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop(), name="delta_stream")

    async def stop(self) -> None:
        """Stop the WebSocket listener and close the connection."""
        self._running = False
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass
            self._ws = None
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    # ------------------------------------------------------------------ #
    # Internal reconnect loop
    # ------------------------------------------------------------------ #

    async def _run_loop(self) -> None:
        """Outer reconnect loop — retries with exponential back-off."""
        while self._running:
            try:
                await self._connect_and_listen()
                self._reconnect_attempts = 0
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                if not self._running:
                    break
                self._reconnect_attempts += 1
                delay = min(
                    _BACKOFF_MAX_SEC,
                    _BACKOFF_BASE_SEC * (2 ** (self._reconnect_attempts - 1)),
                )
                logger.warning(
                    "delta_stream.reconnect",
                    provider=_PROVIDER_ID,
                    symbols=self._symbols,
                    reconnect_attempt=self._reconnect_attempts,
                    delay_sec=delay,
                    reason=str(exc)[:120],
                )
                self._notify_status("reconnecting")
                await asyncio.sleep(delay)

    async def _connect_and_listen(self) -> None:
        """Open the WebSocket, subscribe, and process messages."""
        try:
            import websockets  # noqa: PLC0415
        except ImportError as exc:
            logger.error(
                "delta_stream.websockets_not_installed",
                reason="Install 'websockets' package to use DeltaStreamAdapter",
            )
            raise RuntimeError("websockets package not installed") from exc

        self._notify_status("connecting")
        logger.info(
            "delta_stream.connecting",
            provider=_PROVIDER_ID,
            url=self._ws_url,
            symbols=self._symbols,
        )

        async with websockets.connect(
            self._ws_url,
            ping_interval=_HEARTBEAT_INTERVAL_SEC,
            ping_timeout=5.0,
            close_timeout=5.0,
        ) as ws:
            self._ws = ws
            self._notify_status("open")
            logger.info(
                "delta_stream.connected",
                provider=_PROVIDER_ID,
                symbols=self._symbols,
            )

            # Subscribe to the ticker channel.
            subscribe_msg = json.dumps({
                "type": "subscribe",
                "payload": {
                    "channels": [{"name": "ticker", "symbols": self._symbols}],
                },
            })
            await ws.send(subscribe_msg)
            logger.debug(
                "delta_stream.subscribed",
                provider=_PROVIDER_ID,
                symbols=self._symbols,
            )

            # Message receive loop.
            async for raw_msg in ws:
                if not self._running:
                    break
                try:
                    data = json.loads(raw_msg)
                    tick = _parse_tick(data)
                    if tick is None:
                        continue

                    # Forward to callback.
                    if asyncio.iscoroutinefunction(self._on_tick):
                        await self._on_tick(tick)
                    else:
                        self._on_tick(tick)

                    # Publish to Event Bus if wired.
                    if self._event_bus is not None:
                        try:
                            await self._event_bus.publish("mds:events:ticks", tick)
                        except Exception:  # noqa: BLE001
                            pass

                except json.JSONDecodeError:
                    logger.debug(
                        "delta_stream.non_json_message",
                        provider=_PROVIDER_ID,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "delta_stream.message_processing_error",
                        provider=_PROVIDER_ID,
                        error=str(exc)[:120],
                    )

        self._ws = None
        self._notify_status("closed")

    def _notify_status(self, status: str) -> None:
        if self._on_status_change is not None:
            try:
                result = self._on_status_change(status)
                if asyncio.iscoroutine(result):
                    asyncio.create_task(result)
            except Exception:  # noqa: BLE001
                pass
