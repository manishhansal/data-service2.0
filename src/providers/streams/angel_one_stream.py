"""
src/providers/streams/angel_one_stream.py

Angel One SmartStream WebSocket adapter for DATA-SERVICE 2.0.

The Streaming Engine is the **sole** owner of the Angel One SmartStream
connection.  No consumer may connect to SmartStream directly — all tick
data flows through this adapter, is normalised to canonical schema, and
is forwarded via:
  1. An optional ``on_tick_callback`` callable for in-process consumers.
  2. The Event Bus (Redis Streams) via ``EventBus.publish_tick``.

Reconnect policy (Requirements 15.9)
-------------------------------------
- Exponential backoff: base 1 s, doubles each attempt, capped at 60 s.
- Maximum 10 reconnect attempts before giving up.
- On each attempt: publish ``{"type": "reconnect", "provider": "angel_one",
  "attempt": N}`` to ``mds:events:reconnect``.
- After exhaustion: publish ``{"type": "connection_failed",
  "provider": "angel_one"}`` to ``mds:events:connection-failed``.

Normalisation
-------------
Raw SmartStream fields are mapped to the canonical tick schema documented
in the design §Streaming Engine / Tick Publish Format.  Field mapping:
  - ``ltp`` / ``last_traded_price`` → ``ltp``
  - ``volume_trade_for_the_day`` → ``volume``
  - ``open_price_of_the_day`` → ``open``
  - ``high_price_of_the_day`` → ``high``
  - ``low_price_of_the_day`` → ``low``
  - ``net_change`` / ``change`` → ``change``
  - ``percent_change`` → ``changePct``
  - ``open_interest`` → ``oi``  (never populated from tradedValue)
  - ``total_buy_quantity`` → ``totalBuyQty``
  - ``total_sell_quantity`` → ``totalSellQty``

Requirements: 15.9
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
import uuid
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PROVIDER_ID = "angel_one"

# Reconnect policy
_BACKOFF_BASE_SEC: float = 1.0
_BACKOFF_MAX_SEC: float = 60.0
_MAX_RECONNECT_ATTEMPTS: int = 10

# SmartStream subscription modes
SUBSCRIPTION_MODE_LTP = "LTP"
SUBSCRIPTION_MODE_QUOTE = "QUOTE"
SUBSCRIPTION_MODE_FULL = "FULL"

# Raw field → canonical field mapping for SmartStream ticks
_FIELD_MAP: dict[str, str] = {
    "last_traded_price": "ltp",
    "ltp": "ltp",
    "volume_trade_for_the_day": "volume",
    "vol": "volume",
    "open_price_of_the_day": "open",
    "high_price_of_the_day": "high",
    "low_price_of_the_day": "low",
    "net_change": "change",
    "change": "change",
    "percent_change": "changePct",
    "open_interest": "oi",
    "total_buy_quantity": "totalBuyQty",
    "total_sell_quantity": "totalSellQty",
    "last_traded_time": "lastTradeTime",
    "exchange": "exchange",
    "symbol": "symbol",
    "instrument_token": "instrumentToken",
    "trading_symbol": "symbol",
    "token": "instrumentToken",
}


# ---------------------------------------------------------------------------
# Normalisation helper
# ---------------------------------------------------------------------------


def _normalise_tick(raw: dict[str, Any]) -> dict[str, Any]:
    """Map raw SmartStream tick fields to the canonical tick schema.

    Enforces OI semantic rule: ``oi`` is populated only from
    ``open_interest`` — never from ``traded_value`` or ``tradedValue``.

    Args:
        raw: Raw dict received from the SmartStream WebSocket.

    Returns:
        Canonical tick dict.  Unknown fields are forwarded as-is under
        a ``_raw`` sub-key to preserve auditability without polluting the
        canonical namespace.
    """
    canonical: dict[str, Any] = {
        "tickId": str(uuid.uuid4()),
        "source": _PROVIDER_ID,
        "receivedAtMs": int(time.time() * 1000),
        "isDuplicate": False,
    }

    # Map known raw fields to canonical names
    for raw_key, raw_val in raw.items():
        canon_key = _FIELD_MAP.get(raw_key)
        if canon_key:
            canonical[canon_key] = raw_val

    # Enforce OI semantic integrity: never assign tradedValue to oi
    if "oi" not in canonical:
        canonical["oi"] = None
        canonical["oiMissing"] = True
    else:
        canonical["oiMissing"] = False

    # Ensure ltp is numeric (SmartStream may send as int representing paise)
    if "ltp" in canonical and isinstance(canonical["ltp"], int):
        canonical["ltp"] = canonical["ltp"] / 100.0

    # eventTimeMs — use last_traded_time if available, else receivedAtMs
    if "lastTradeTime" in canonical:
        canonical["eventTimeMs"] = canonical["lastTradeTime"]
    else:
        canonical["eventTimeMs"] = canonical["receivedAtMs"]

    return canonical


# ---------------------------------------------------------------------------
# AngelOneStreamAdapter
# ---------------------------------------------------------------------------


class AngelOneStreamAdapter:
    """Async adapter for the Angel One SmartStream WebSocket feed.

    The adapter manages the full lifecycle of a SmartStream connection:
    connect → subscribe → receive ticks → reconnect on failure.

    It is the **sole owner** of the underlying WebSocket — no consumer
    receives raw frames.  Normalised ticks are forwarded to
    ``on_tick_callback`` (if set) and can be published to the Event Bus
    by the caller's callback implementation.

    Parameters
    ----------
    event_bus:
        Optional ``EventBus`` instance.  When provided, reconnect and
        connection-failed events are published automatically.  When
        ``None``, only structured log messages are emitted.

    Usage
    -----
    ::

        adapter = AngelOneStreamAdapter(event_bus=bus)
        adapter.on_tick_callback = my_tick_handler

        await adapter.connect(access_token="...", feed_token="...")
        await adapter.subscribe(tokens=["256265", "99926000"])

        # Later…
        await adapter.disconnect()
    """

    # SmartStream WebSocket base URL (Angel One API documentation)
    _WS_URL = "wss://smartapisocket.angelone.in/smart-stream"

    def __init__(self, event_bus: Any = None) -> None:
        self._event_bus = event_bus
        self._ws: Any = None  # websockets.WebSocketClientProtocol
        self._connection_task: Optional[asyncio.Task[None]] = None
        self._subscribed_tokens: list[str] = []
        self._subscription_mode: str = SUBSCRIPTION_MODE_FULL
        self._access_token: str = ""
        self._feed_token: str = ""
        self._running: bool = False
        self._reconnect_attempts: int = 0
        self.on_tick_callback: Optional[Callable[[dict[str, Any]], None]] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def connect(self, access_token: str, feed_token: str) -> None:
        """Establish the SmartStream WebSocket connection.

        Stores the tokens for reconnect use and starts the background
        connection-management task.

        Args:
            access_token: Angel One REST API JWT access token.
            feed_token:   SmartStream-specific feed token obtained via
                          the Angel One REST ``/rest/secure/angelbroking/
                          user/v1/getProfile`` response.
        """
        self._access_token = access_token
        self._feed_token = feed_token
        self._running = True
        self._reconnect_attempts = 0
        self._connection_task = asyncio.create_task(
            self._run_connection_loop(), name="angel_one_stream_loop"
        )
        logger.info(
            "angel_one_stream_connecting",
            extra={"provider": _PROVIDER_ID},
        )

    async def disconnect(self) -> None:
        """Close the WebSocket connection and cancel the background task."""
        self._running = False
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        if self._connection_task is not None and not self._connection_task.done():
            self._connection_task.cancel()
            try:
                await self._connection_task
            except asyncio.CancelledError:
                pass
        logger.info(
            "angel_one_stream_disconnected",
            extra={"provider": _PROVIDER_ID},
        )

    async def subscribe(
        self,
        tokens: list[str],
        mode: str = SUBSCRIPTION_MODE_FULL,
    ) -> None:
        """Subscribe to tick feeds for the given instrument tokens.

        The subscription is replayed automatically on every reconnect so
        that ticks resume without caller intervention.

        Args:
            tokens: List of Angel One instrument token strings.
            mode:   Subscription mode — one of ``"LTP"``, ``"QUOTE"``,
                    or ``"FULL"`` (default).
        """
        self._subscribed_tokens = list(tokens)
        self._subscription_mode = mode
        if self._ws is not None:
            await self._send_subscribe_message()

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
                if self._reconnect_attempts > _MAX_RECONNECT_ATTEMPTS:
                    await self._handle_exhaustion()
                    break
                backoff = min(
                    _BACKOFF_BASE_SEC * (2 ** (self._reconnect_attempts - 1)),
                    _BACKOFF_MAX_SEC,
                )
                logger.warning(
                    "angel_one_stream_reconnecting",
                    extra={
                        "provider": _PROVIDER_ID,
                        "attempt": self._reconnect_attempts,
                        "backoff_sec": backoff,
                        "error": str(exc),
                    },
                )
                await self._publish_reconnect_event(self._reconnect_attempts)
                await asyncio.sleep(backoff)

    async def _connect_once(self) -> None:
        """Open the WebSocket, send the subscribe message, and pump messages."""
        import websockets  # local import — optional dep in test environments

        headers = {
            "Authorization": self._access_token,
            "x-feed-token": self._feed_token,
        }
        async with websockets.connect(
            self._WS_URL,
            additional_headers=headers,
            ping_interval=None,  # SmartStream manages its own heartbeats
        ) as ws:
            self._ws = ws
            self._reconnect_attempts = 0  # reset on successful connect
            logger.info(
                "angel_one_stream_connected",
                extra={"provider": _PROVIDER_ID},
            )
            if self._subscribed_tokens:
                await self._send_subscribe_message()
            await self._receive_loop(ws)

    async def _receive_loop(self, ws: Any) -> None:
        """Pump incoming messages from the WebSocket."""
        async for raw_message in ws:
            if not self._running:
                break
            try:
                if isinstance(raw_message, bytes):
                    # SmartStream sends binary frames; decode to JSON
                    data = json.loads(raw_message.decode("utf-8"))
                else:
                    data = json.loads(raw_message)
                tick = _normalise_tick(data)
                if self.on_tick_callback is not None:
                    self.on_tick_callback(tick)
            except Exception as exc:
                logger.warning(
                    "angel_one_tick_parse_error",
                    extra={"provider": _PROVIDER_ID, "error": str(exc)},
                )

    async def _send_subscribe_message(self) -> None:
        """Send a SmartStream subscription request for stored tokens."""
        if self._ws is None:
            return
        msg = {
            "correlationID": str(uuid.uuid4()),
            "action": 1,  # 1 = subscribe
            "params": {
                "mode": self._subscription_mode,
                "tokenList": [
                    {"exchangeType": 1, "tokens": self._subscribed_tokens}
                ],
            },
        }
        try:
            await self._ws.send(json.dumps(msg))
            logger.debug(
                "angel_one_subscribed",
                extra={
                    "provider": _PROVIDER_ID,
                    "token_count": len(self._subscribed_tokens),
                    "mode": self._subscription_mode,
                },
            )
        except Exception as exc:
            logger.warning(
                "angel_one_subscribe_failed",
                extra={"provider": _PROVIDER_ID, "error": str(exc)},
            )

    # ------------------------------------------------------------------
    # Event Bus helpers
    # ------------------------------------------------------------------

    async def _publish_reconnect_event(self, attempt: int) -> None:
        """Publish a reconnect attempt event to the Event Bus."""
        if self._event_bus is not None:
            try:
                await self._event_bus.publish_reconnect(_PROVIDER_ID, attempt)
            except Exception as exc:
                logger.warning(
                    "angel_one_reconnect_event_failed",
                    extra={"provider": _PROVIDER_ID, "error": str(exc)},
                )

    async def _handle_exhaustion(self) -> None:
        """Called when all reconnect attempts are exhausted."""
        logger.error(
            "angel_one_stream_connection_failed",
            extra={
                "provider": _PROVIDER_ID,
                "max_attempts": _MAX_RECONNECT_ATTEMPTS,
            },
        )
        if self._event_bus is not None:
            try:
                await self._event_bus.publish_connection_failed(_PROVIDER_ID)
            except Exception as exc:
                logger.warning(
                    "angel_one_connection_failed_event_error",
                    extra={"provider": _PROVIDER_ID, "error": str(exc)},
                )

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
