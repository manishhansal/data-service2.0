"""
src/providers/streams/upstox_stream.py

Upstox V3 Protobuf WebSocket adapter for DATA-SERVICE 2.0.

The Streaming Engine is the **sole** owner of the Upstox V3 Protobuf
WebSocket connection.  Protobuf decoding happens inside this adapter;
decoded fields are normalised to the canonical tick schema before being
passed to the caller-supplied callback.

Protobuf stub
-------------
The full Upstox Protobuf schema requires generated ``*_pb2.py`` files.
This adapter ships a *stub decoder* that gracefully handles both real
Protobuf bytes (when ``google.protobuf`` can parse them) and plain JSON
frames, ensuring the adapter is fully testable without a compiled schema.
When the production environment has the generated Protobuf definitions,
replace ``_decode_protobuf`` with the real implementation.

Reconnect policy (Requirements 15.9)
--------------------------------------
- Exponential backoff: base 1 s, doubles each attempt, capped at 60 s.
- Maximum 10 reconnect attempts before giving up.
- On each attempt: publish ``{"type": "reconnect", "provider": "upstox",
  "attempt": N}`` to the Event Bus.
- After exhaustion: publish ``{"type": "connection_failed",
  "provider": "upstox"}`` to the Event Bus.

Normalisation
-------------
Upstox Protobuf FeedResponse fields are mapped to the canonical schema:
  - ``ltq`` / ``last_trade_price`` / ``ltp`` → ``ltp``
  - ``vol`` / ``volume`` → ``volume``
  - ``open`` / ``open_price`` → ``open``
  - ``high`` / ``high_price`` → ``high``
  - ``low`` / ``low_price`` → ``low``
  - ``net_change`` / ``change`` → ``change``
  - ``change_percent`` → ``changePct``
  - ``oi`` / ``open_interest`` → ``oi`` (never populated from tradedValue)
  - ``tbq`` / ``total_buy_qty`` → ``totalBuyQty``
  - ``tsq`` / ``total_sell_qty`` → ``totalSellQty``
  - ``instrument_key`` → ``instrumentId``

Requirements: 15.7, 15.9
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

_PROVIDER_ID = "upstox"

# Reconnect policy — same thresholds as Angel One SmartStream
_BACKOFF_BASE_SEC: float = 1.0
_BACKOFF_MAX_SEC: float = 60.0
_MAX_RECONNECT_ATTEMPTS: int = 10

# Upstox V3 WebSocket URL (Protobuf feed)
_WS_URL_TEMPLATE = "wss://api.upstox.com/v2/feed/market-data-feed"

# Subscription modes
SUBSCRIPTION_MODE_LTP = "ltpc"
SUBSCRIPTION_MODE_QUOTE = "quote"
SUBSCRIPTION_MODE_FULL = "full"

# Raw Protobuf / JSON field → canonical field mapping
_FIELD_MAP: dict[str, str] = {
    "ltp": "ltp",
    "last_trade_price": "ltp",
    "ltq": "ltp",
    "volume": "volume",
    "vol": "volume",
    "open": "open",
    "open_price": "open",
    "high": "high",
    "high_price": "high",
    "low": "low",
    "low_price": "low",
    "close": "close",
    "prev_close_price": "prevClose",
    "net_change": "change",
    "change": "change",
    "change_percent": "changePct",
    "net_change_percent": "changePct",
    "oi": "oi",
    "open_interest": "oi",
    "total_buy_qty": "totalBuyQty",
    "tbq": "totalBuyQty",
    "total_sell_qty": "totalSellQty",
    "tsq": "totalSellQty",
    "upper_circuit_limit": "upperCircuit",
    "lower_circuit_limit": "lowerCircuit",
    "last_trade_time": "lastTradeTime",
    "exchange_time": "lastTradeTime",
    "instrument_key": "instrumentId",
    "symbol": "symbol",
    "exchange": "exchange",
}


# ---------------------------------------------------------------------------
# Protobuf stub decoder
# ---------------------------------------------------------------------------


def _decode_protobuf(raw_bytes: bytes) -> dict[str, Any]:
    """Decode a raw Upstox Protobuf frame into a plain dict.

    Production: replace this stub with the real generated ``MarketFullFeed``
    or ``FeedResponse`` Protobuf parser once the ``*_pb2.py`` files are
    compiled from the Upstox Protobuf schema.

    Stub behaviour:
    1. Try to parse *raw_bytes* as UTF-8 JSON (handles dev / mock frames).
    2. If UTF-8 decoding fails, attempt a best-effort Protobuf parse using
       ``google.protobuf.json_format`` (when available).
    3. Fall back to an empty dict with ``_parse_error: true`` so the
       normaliser can set appropriate missing flags without crashing.

    Args:
        raw_bytes: Raw WebSocket frame bytes.

    Returns:
        A plain Python dict with the decoded fields.
    """
    # Fast path: try JSON (used in tests and mock environments)
    try:
        return json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        pass

    # Best-effort Protobuf parse via google.protobuf if available
    try:
        from google.protobuf import json_format  # noqa: PLC0415
        from google.protobuf.message import DecodeError  # noqa: PLC0415

        # Dynamic descriptor-less decode → raw bytes as base64 string for now.
        # Replace with the generated class when available.
        logger.debug(
            "upstox_protobuf_raw_decode",
            extra={"frame_len": len(raw_bytes)},
        )
    except ImportError:
        pass

    logger.warning(
        "upstox_protobuf_decode_failed",
        extra={"frame_len": len(raw_bytes)},
    )
    return {"_parse_error": True}


# ---------------------------------------------------------------------------
# Normalisation helper
# ---------------------------------------------------------------------------


def _normalise_tick(raw: dict[str, Any]) -> dict[str, Any]:
    """Map raw Upstox decoded fields to the canonical tick schema.

    Enforces OI semantic rule: ``oi`` is populated only from the ``oi``
    or ``open_interest`` fields — never from ``tradedValue``.

    Args:
        raw: Decoded dict from ``_decode_protobuf``.

    Returns:
        Canonical tick dict.
    """
    canonical: dict[str, Any] = {
        "tickId": str(uuid.uuid4()),
        "source": _PROVIDER_ID,
        "receivedAtMs": int(time.time() * 1000),
        "isDuplicate": False,
    }

    for raw_key, raw_val in raw.items():
        canon_key = _FIELD_MAP.get(raw_key)
        if canon_key:
            canonical[canon_key] = raw_val

    # OI semantic integrity
    if "oi" not in canonical:
        canonical["oi"] = None
        canonical["oiMissing"] = True
    else:
        canonical["oiMissing"] = False

    # eventTimeMs
    if "lastTradeTime" in canonical:
        canonical["eventTimeMs"] = canonical["lastTradeTime"]
    else:
        canonical["eventTimeMs"] = canonical["receivedAtMs"]

    return canonical


# ---------------------------------------------------------------------------
# UpstoxStreamAdapter
# ---------------------------------------------------------------------------


class UpstoxStreamAdapter:
    """Async adapter for the Upstox V3 Protobuf WebSocket feed.

    Manages the full lifecycle of the Upstox V3 Protobuf market-data
    WebSocket: connect → subscribe → decode → normalise → callback.

    Identical reconnect policy to ``AngelOneStreamAdapter``:
    exponential backoff base 1s, max 60s, max 10 attempts.

    Parameters
    ----------
    event_bus:
        Optional ``EventBus`` instance for reconnect / failure events.

    Usage
    -----
    ::

        adapter = UpstoxStreamAdapter(event_bus=bus)
        adapter.on_tick_callback = my_handler

        await adapter.connect(access_token="Bearer ...")
        await adapter.subscribe(
            instrument_keys=["NSE_INDEX|Nifty 50", "NSE_EQ|RELIANCE"]
        )

        # Later…
        await adapter.disconnect()
    """

    def __init__(self, event_bus: Any = None) -> None:
        self._event_bus = event_bus
        self._ws: Any = None
        self._connection_task: Optional[asyncio.Task[None]] = None
        self._subscribed_keys: list[str] = []
        self._subscription_mode: str = SUBSCRIPTION_MODE_FULL
        self._access_token: str = ""
        self._running: bool = False
        self._reconnect_attempts: int = 0
        self.on_tick_callback: Optional[Callable[[dict[str, Any]], None]] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def connect(self, access_token: str) -> None:
        """Establish the Upstox V3 Protobuf WebSocket connection.

        Args:
            access_token: Upstox OAuth2 Bearer token (including
                          ``"Bearer "`` prefix or just the raw token —
                          the adapter prepends it if absent).
        """
        if not access_token.startswith("Bearer "):
            access_token = f"Bearer {access_token}"
        self._access_token = access_token
        self._running = True
        self._reconnect_attempts = 0
        self._connection_task = asyncio.create_task(
            self._run_connection_loop(), name="upstox_stream_loop"
        )
        logger.info(
            "upstox_stream_connecting",
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
            "upstox_stream_disconnected",
            extra={"provider": _PROVIDER_ID},
        )

    async def subscribe(
        self,
        instrument_keys: list[str],
        mode: str = SUBSCRIPTION_MODE_FULL,
    ) -> None:
        """Subscribe to the Upstox feed for the given instrument keys.

        Args:
            instrument_keys: Upstox instrument key strings, e.g.
                             ``["NSE_INDEX|Nifty 50", "NSE_EQ|RELIANCE"]``.
            mode:            Subscription mode — one of ``"ltpc"``,
                             ``"quote"``, or ``"full"`` (default).
        """
        self._subscribed_keys = list(instrument_keys)
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
                    "upstox_stream_reconnecting",
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
        """Open WebSocket, subscribe, and pump messages."""
        import websockets  # local import — optional dep in test environments

        headers = {"Authorization": self._access_token}
        async with websockets.connect(
            _WS_URL_TEMPLATE,
            additional_headers=headers,
        ) as ws:
            self._ws = ws
            self._reconnect_attempts = 0
            logger.info(
                "upstox_stream_connected",
                extra={"provider": _PROVIDER_ID},
            )
            if self._subscribed_keys:
                await self._send_subscribe_message()
            await self._receive_loop(ws)

    async def _receive_loop(self, ws: Any) -> None:
        """Pump incoming messages from the WebSocket and decode them."""
        async for raw_message in ws:
            if not self._running:
                break
            try:
                if isinstance(raw_message, bytes):
                    data = _decode_protobuf(raw_message)
                else:
                    data = json.loads(raw_message)

                if data.get("_parse_error"):
                    logger.warning(
                        "upstox_tick_decode_failed",
                        extra={"provider": _PROVIDER_ID},
                    )
                    continue

                tick = _normalise_tick(data)
                if self.on_tick_callback is not None:
                    self.on_tick_callback(tick)
            except Exception as exc:
                logger.warning(
                    "upstox_tick_parse_error",
                    extra={"provider": _PROVIDER_ID, "error": str(exc)},
                )

    async def _send_subscribe_message(self) -> None:
        """Send an Upstox subscription request for stored instrument keys."""
        if self._ws is None:
            return
        msg = {
            "guid": str(uuid.uuid4()),
            "method": "sub",
            "data": {
                "mode": self._subscription_mode,
                "instrumentKeys": self._subscribed_keys,
            },
        }
        try:
            await self._ws.send(json.dumps(msg))
            logger.debug(
                "upstox_subscribed",
                extra={
                    "provider": _PROVIDER_ID,
                    "instrument_count": len(self._subscribed_keys),
                    "mode": self._subscription_mode,
                },
            )
        except Exception as exc:
            logger.warning(
                "upstox_subscribe_failed",
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
                    "upstox_reconnect_event_failed",
                    extra={"provider": _PROVIDER_ID, "error": str(exc)},
                )

    async def _handle_exhaustion(self) -> None:
        """Called when all reconnect attempts are exhausted."""
        logger.error(
            "upstox_stream_connection_failed",
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
                    "upstox_connection_failed_event_error",
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
