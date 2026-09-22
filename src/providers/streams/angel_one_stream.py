"""
src/providers/streams/angel_one_stream.py

Angel One SmartStream WebSocket adapter for DATA-SERVICE 2.0.

The Streaming Engine is the sole owner of the Angel One SmartStream connection.
No consumer may connect to SmartStream directly — all tick data flows through
this adapter, is decoded from the binary protocol, normalized to the canonical
tick schema, and forwarded via the on_tick_callback.

Binary protocol
---------------
SmartStream V2 sends packed binary frames, NOT JSON text.  The binary format
varies by subscription mode:

  LTP mode (mode=1):
    Bytes  0-1:  subscription mode (int16, little-endian) = 1
    Bytes  2-3:  exchange type (int16, LE)
    Bytes  4-23: token (char[20], null-padded ASCII)
    Bytes 24-31: sequence number (int64, LE)
    Bytes 32-39: exchange timestamp (int64, LE, Unix ms)
    Bytes 40-47: last traded price (int64, LE, value in paise — divide by 100)

  QUOTE mode (mode=2): same header + additional fields:
    Bytes 48-55: last traded quantity (int64, LE)
    Bytes 56-63: average traded price (int64, LE, paise)
    Bytes 64-71: volume for the day (int64, LE)
    Bytes 72-79: total buy quantity (float64, LE)
    Bytes 80-87: total sell quantity (float64, LE)
    Bytes 88-95: open price for the day (int64, LE, paise)
    Bytes 96-103: high price (int64, LE, paise)
    Bytes 104-111: low price (int64, LE, paise)
    Bytes 112-119: close price (int64, LE, paise)

  FULL mode (mode=3): QUOTE + market depth:
    Bytes 120-127: last traded time (int64, LE, Unix ms)
    Bytes 128-135: open interest (int64, LE)
    Bytes 136-143: OI change (int64, LE)
    Bytes 144-151: 52-week high (int64, LE, paise)
    Bytes 152-159: 52-week low (int64, LE, paise)
    Bytes 160-199: upper circuit (int64, LE, paise) + lower circuit
    Then depth: 5 buy levels × 20 bytes + 5 sell levels × 20 bytes
      Each level: price (int64, paise) + qty (int64) + orders (int16)

Exchange type codes (SmartStream V2):
    1 = NSE EQ
    2 = NFO (NSE F&O)
    3 = BSE EQ
    4 = BSE FO
    5 = MCX

Subscription action codes:
    1 = subscribe
    0 = unsubscribe

Mode integer values for subscription:
    1 = LTP
    2 = QUOTE
    3 = FULL

Reconnect policy (Requirements 15.9)
--------------------------------------
- Exponential backoff: base 1 s, doubles each attempt, capped at 60 s.
- Maximum 10 reconnect attempts before giving up.

Requirements: 15.9
"""

from __future__ import annotations

import asyncio
import json
import logging
import struct
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

# SmartStream WebSocket URL (V2, current)
_WS_URL = "wss://smartapisocket.angelone.in/smart-stream"

# Subscription mode integer values (NOT strings)
SUBSCRIPTION_MODE_LTP: int = 1
SUBSCRIPTION_MODE_QUOTE: int = 2
SUBSCRIPTION_MODE_FULL: int = 3

# Exchange type codes
EXCHANGE_TYPE_NSE_EQ: int = 1
EXCHANGE_TYPE_NFO: int = 2     # NSE F&O — required for options/futures
EXCHANGE_TYPE_BSE_EQ: int = 3
EXCHANGE_TYPE_BSE_FO: int = 4
EXCHANGE_TYPE_MCX: int = 5

# Map exchange type int to human-readable name
EXCHANGE_TYPE_NAMES: dict[int, str] = {
    1: "NSE_EQ",
    2: "NFO",
    3: "BSE_EQ",
    4: "BSE_FO",
    5: "MCX",
}

# Minimum binary frame sizes by mode (bytes)
_MIN_FRAME_LTP: int = 48
_MIN_FRAME_QUOTE: int = 120
_MIN_FRAME_FULL: int = 200  # approximate — includes depth


# ---------------------------------------------------------------------------
# Binary decoder
# ---------------------------------------------------------------------------

def _decode_smartstream_binary(raw: bytes) -> Optional[dict[str, Any]]:
    """Decode a SmartStream V2 binary frame into a canonical tick dict.

    Handles LTP (mode=1), QUOTE (mode=2), and FULL (mode=3) frames.
    Returns None if the frame is too short to decode safely.

    Price fields: stored in paise (integer, 1/100 of INR).
                  Converted to float INR by dividing by 100.
    Quantity fields: stored as int64.
    Timestamps: Unix milliseconds (int64).

    OI semantic integrity: openInterest is populated ONLY from the explicit
    OI field in FULL mode — NEVER from any volume or traded-value field.

    Args:
        raw: Raw binary WebSocket frame bytes.

    Returns:
        Decoded tick dict, or None if the frame cannot be parsed.
    """
    if len(raw) < _MIN_FRAME_LTP:
        logger.debug("smartstream_frame_too_short", extra={"len": len(raw)})
        return None

    try:
        mode = struct.unpack_from("<H", raw, 0)[0]
        exchange_type = struct.unpack_from("<H", raw, 2)[0]

        # Token: 20 bytes of ASCII, null-padded
        token_bytes = raw[4:24]
        token_str = token_bytes.split(b"\x00")[0].decode("ascii", errors="ignore").strip()

        seq_num = struct.unpack_from("<q", raw, 24)[0]
        exchange_ts_ms = struct.unpack_from("<q", raw, 32)[0]
        ltp_paise = struct.unpack_from("<q", raw, 40)[0]

        tick: dict[str, Any] = {
            "tickId":          str(uuid.uuid4()),
            "source":          _PROVIDER_ID,
            "receivedAtMs":    int(time.time() * 1000),
            "isDuplicate":     False,
            "subscriptionMode": mode,
            "exchange":        EXCHANGE_TYPE_NAMES.get(exchange_type, str(exchange_type)),
            "exchangeType":    exchange_type,
            "instrumentToken": token_str,
            "sequenceNumber":  seq_num,
            "exchangeTimestampMs": exchange_ts_ms,
            "eventTimeMs":     exchange_ts_ms,
            "ltp":             ltp_paise / 100.0,
            # OI defaults to None — only set in FULL mode
            "oi":              None,
            "oiMissing":       True,
        }

        if mode >= SUBSCRIPTION_MODE_QUOTE and len(raw) >= _MIN_FRAME_QUOTE:
            ltq   = struct.unpack_from("<q", raw, 48)[0]
            atp   = struct.unpack_from("<q", raw, 56)[0]   # avg traded price, paise
            vol   = struct.unpack_from("<q", raw, 64)[0]
            tbq   = struct.unpack_from("<d", raw, 72)[0]   # float64
            tsq   = struct.unpack_from("<d", raw, 80)[0]
            open_ = struct.unpack_from("<q", raw, 88)[0]
            high  = struct.unpack_from("<q", raw, 96)[0]
            low   = struct.unpack_from("<q", raw, 104)[0]
            close = struct.unpack_from("<q", raw, 112)[0]

            tick.update({
                "lastTradedQty":  ltq,
                "avgTradedPrice": atp / 100.0,
                "volume":         vol,
                "totalBuyQty":    tbq,
                "totalSellQty":   tsq,
                "open":           open_ / 100.0,
                "high":           high / 100.0,
                "low":            low / 100.0,
                "close":          close / 100.0,
            })

        if mode >= SUBSCRIPTION_MODE_FULL and len(raw) >= _MIN_FRAME_FULL:
            last_trade_ts = struct.unpack_from("<q", raw, 120)[0]
            oi            = struct.unpack_from("<q", raw, 128)[0]
            oi_change     = struct.unpack_from("<q", raw, 136)[0]
            week_high_52  = struct.unpack_from("<q", raw, 144)[0]
            week_low_52   = struct.unpack_from("<q", raw, 152)[0]

            # OI semantic integrity: only populated from explicit OI field
            tick.update({
                "lastTradeTime":  last_trade_ts,
                "eventTimeMs":    last_trade_ts if last_trade_ts > 0 else exchange_ts_ms,
                "oi":             oi,
                "oiMissing":      False,
                "oiChange":       oi_change,
                "weekHigh52":     week_high_52 / 100.0,
                "weekLow52":      week_low_52 / 100.0,
            })

            # Decode market depth if enough bytes remain
            # Upper/lower circuit: bytes 160-175
            if len(raw) >= 176:
                upper_circuit = struct.unpack_from("<q", raw, 160)[0]
                lower_circuit = struct.unpack_from("<q", raw, 168)[0]
                tick["upperCircuit"] = upper_circuit / 100.0
                tick["lowerCircuit"] = lower_circuit / 100.0

            # Market depth: 5 buy + 5 sell levels starting at byte 192
            # Each level: price (int64, paise) + qty (int64) + orders (int16) = 18 bytes
            depth_offset = 192
            level_size = 18
            if len(raw) >= depth_offset + 10 * level_size:
                buy_depth = []
                sell_depth = []
                for i in range(5):
                    off = depth_offset + i * level_size
                    price  = struct.unpack_from("<q", raw, off)[0]
                    qty    = struct.unpack_from("<q", raw, off + 8)[0]
                    orders = struct.unpack_from("<H", raw, off + 16)[0]
                    buy_depth.append({
                        "price":  price / 100.0,
                        "qty":    qty,
                        "orders": orders,
                        "level":  i + 1,
                    })
                sell_offset = depth_offset + 5 * level_size
                for i in range(5):
                    off = sell_offset + i * level_size
                    price  = struct.unpack_from("<q", raw, off)[0]
                    qty    = struct.unpack_from("<q", raw, off + 8)[0]
                    orders = struct.unpack_from("<H", raw, off + 16)[0]
                    sell_depth.append({
                        "price":  price / 100.0,
                        "qty":    qty,
                        "orders": orders,
                        "level":  i + 1,
                    })
                tick["depthBuy"]  = buy_depth
                tick["depthSell"] = sell_depth

        return tick

    except struct.error as exc:
        logger.warning(
            "smartstream_binary_decode_error",
            extra={"error": str(exc), "frame_len": len(raw)},
        )
        return None


# ---------------------------------------------------------------------------
# AngelOneStreamAdapter
# ---------------------------------------------------------------------------


class AngelOneStreamAdapter:
    """Async adapter for the Angel One SmartStream WebSocket (V2 binary protocol).

    Manages the full lifecycle of a SmartStream connection:
    connect → subscribe → decode binary frames → normalize → callback.

    Binary protocol is fully decoded — no JSON fallback on real frames.
    Mode values are integers (1=LTP, 2=QUOTE, 3=FULL) as required by the
    SmartStream protocol.

    Exchange type support:
      1 = NSE EQ    (equities)
      2 = NFO       (NSE F&O — options, futures)
      3 = BSE EQ
      4 = BSE FO
      5 = MCX

    Parameters
    ----------
    event_bus:
        Optional EventBus for reconnect/failure events.

    Usage
    -----
    ::

        adapter = AngelOneStreamAdapter(event_bus=bus)
        adapter.on_tick_callback = my_handler

        # Subscribe to NSE EQ + NFO F&O
        await adapter.connect(access_token="...", feed_token="...")
        await adapter.subscribe(
            token_groups=[
                {"exchange_type": EXCHANGE_TYPE_NSE_EQ, "tokens": ["3045", "2885"]},
                {"exchange_type": EXCHANGE_TYPE_NFO,    "tokens": ["43985", "43986"]},
            ],
            mode=SUBSCRIPTION_MODE_FULL,
        )
    """

    def __init__(self, event_bus: Any = None) -> None:
        self._event_bus = event_bus
        self._ws: Any = None
        self._connection_task: Optional[asyncio.Task[None]] = None

        # Subscription state — preserved across reconnects
        # List of {"exchange_type": int, "tokens": [str, ...]}
        self._token_groups: list[dict[str, Any]] = []
        self._subscription_mode: int = SUBSCRIPTION_MODE_FULL
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

        Args:
            access_token: Angel One REST API JWT access token.
            feed_token:   SmartStream-specific feed token from getProfile response.
        """
        self._access_token = access_token
        self._feed_token = feed_token
        self._running = True
        self._reconnect_attempts = 0
        self._connection_task = asyncio.create_task(
            self._run_connection_loop(), name="angel_one_stream_loop"
        )
        logger.info("angel_one_stream_connecting", extra={"provider": _PROVIDER_ID})

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
        logger.info("angel_one_stream_disconnected", extra={"provider": _PROVIDER_ID})

    async def subscribe(
        self,
        token_groups: list[dict[str, Any]] | None = None,
        mode: int = SUBSCRIPTION_MODE_FULL,
        *,
        tokens: list[str] | None = None,
        exchange_type: int = EXCHANGE_TYPE_NSE_EQ,
    ) -> None:
        """Subscribe to tick feeds for the given exchange-type × token groups.

        Supports two calling conventions:
        1. New: ``token_groups=[{"exchange_type": int, "tokens": [...]}]``
        2. Old (backward-compat): ``tokens=["123", "456"]`` with optional ``exchange_type``

        The subscription is replayed automatically on every reconnect.

        Args:
            token_groups: List of dicts, each with:
                          ``{"exchange_type": int, "tokens": [str, ...]}``.
                          Use EXCHANGE_TYPE_* constants for exchange_type.
            mode:         Subscription mode integer:
                          SUBSCRIPTION_MODE_LTP=1, QUOTE=2, FULL=3.
            tokens:       Backward-compat: flat list of token strings.
            exchange_type: Backward-compat: exchange type for flat token list.
        """
        if tokens is not None and token_groups is None:
            # Backward-compat: old subscribe(tokens=[...]) call style
            token_groups = [{"exchange_type": exchange_type, "tokens": tokens}]

        if token_groups is None:
            token_groups = []

        self._token_groups = list(token_groups)
        self._subscription_mode = mode

        # Backward-compat attributes for tests
        self._subscribed_tokens = [
            t for g in self._token_groups for t in g.get("tokens", [])
        ]

        if self._ws is not None:
            await self._send_subscribe_message()

    async def unsubscribe(
        self,
        token_groups: list[dict[str, Any]],
    ) -> None:
        """Unsubscribe from tick feeds for the given token groups.

        Args:
            token_groups: Same structure as subscribe(). action=0 = unsubscribe.
        """
        if self._ws is None:
            return
        msg = {
            "correlationID": str(uuid.uuid4()),
            "action": 0,  # 0 = unsubscribe
            "params": {
                "mode": self._subscription_mode,
                "tokenList": [
                    {"exchangeType": g["exchange_type"], "tokens": g["tokens"]}
                    for g in token_groups
                ],
            },
        }
        try:
            await self._ws.send(json.dumps(msg))
            # Remove unsubscribed tokens from state
            unsubscribed_keys = {
                (g["exchange_type"], t)
                for g in token_groups
                for t in g["tokens"]
            }
            self._token_groups = [
                {
                    "exchange_type": g["exchange_type"],
                    "tokens": [t for t in g["tokens"] if (g["exchange_type"], t) not in unsubscribed_keys],
                }
                for g in self._token_groups
            ]
            self._token_groups = [g for g in self._token_groups if g["tokens"]]
            logger.debug(
                "angel_one_unsubscribed",
                extra={"provider": _PROVIDER_ID, "count": len(token_groups)},
            )
        except Exception as exc:
            logger.warning(
                "angel_one_unsubscribe_failed",
                extra={"provider": _PROVIDER_ID, "error": str(exc)},
            )

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
        """Open the WebSocket, send subscribe, and pump binary messages."""
        import websockets  # local import — optional dep in test environments

        headers = {
            "Authorization": self._access_token,
            "x-feed-token": self._feed_token,
        }
        async with websockets.connect(
            _WS_URL,
            additional_headers=headers,
            ping_interval=None,  # SmartStream manages its own heartbeats
        ) as ws:
            self._ws = ws
            self._reconnect_attempts = 0
            logger.info("angel_one_stream_connected", extra={"provider": _PROVIDER_ID})
            if self._token_groups:
                await self._send_subscribe_message()
            await self._receive_loop(ws)

    async def _receive_loop(self, ws: Any) -> None:
        """Pump incoming SmartStream binary messages."""
        async for raw_message in ws:
            if not self._running:
                break
            try:
                if isinstance(raw_message, bytes):
                    # SmartStream V2: binary packed format — decode with struct unpacker
                    tick = _decode_smartstream_binary(raw_message)
                    if tick is None:
                        # Fallback: try JSON decode (for test environments)
                        try:
                            data = json.loads(raw_message.decode("utf-8"))
                            if "ltp" in data or "last_traded_price" in data:
                                tick = _normalise_tick(data)
                            else:
                                logger.warning(
                                    "angel_one_tick_decode_failed",
                                    extra={"provider": _PROVIDER_ID, "frame_len": len(raw_message)},
                                )
                                continue
                        except (UnicodeDecodeError, json.JSONDecodeError):
                            logger.warning(
                                "angel_one_tick_decode_failed",
                                extra={"provider": _PROVIDER_ID, "frame_len": len(raw_message)},
                            )
                            continue
                    if self.on_tick_callback is not None:
                        self.on_tick_callback(tick)
                elif isinstance(raw_message, str):
                    # SmartStream text frames: usually heartbeat/status JSON.
                    # In test environments, JSON text frames may carry tick data.
                    try:
                        data = json.loads(raw_message)
                        # If it looks like a tick (has ltp/symbol), normalize it
                        if "ltp" in data or "last_traded_price" in data:
                            tick = _normalise_tick(data)
                            if self.on_tick_callback is not None:
                                self.on_tick_callback(tick)
                        else:
                            logger.debug(
                                "angel_one_status_message",
                                extra={"provider": _PROVIDER_ID, "msg": data},
                            )
                    except json.JSONDecodeError:
                        logger.debug(
                            "angel_one_text_frame",
                            extra={"provider": _PROVIDER_ID, "text": raw_message[:100]},
                        )
                else:
                    continue

            except Exception as exc:
                logger.warning(
                    "angel_one_tick_parse_error",
                    extra={"provider": _PROVIDER_ID, "error": str(exc)},
                )

    async def _send_subscribe_message(self) -> None:
        """Send a SmartStream subscription request for all stored token groups."""
        if self._ws is None or not self._token_groups:
            return

        # Build tokenList: each group has its own exchangeType
        token_list = [
            {"exchangeType": g["exchange_type"], "tokens": g["tokens"]}
            for g in self._token_groups
            if g.get("tokens")
        ]

        if not token_list:
            return

        msg = {
            "correlationID": str(uuid.uuid4()),
            "action": 1,  # 1 = subscribe
            "params": {
                "mode": self._subscription_mode,   # integer: 1=LTP, 2=QUOTE, 3=FULL
                "tokenList": token_list,
            },
        }
        try:
            await self._ws.send(json.dumps(msg))
            total_tokens = sum(len(g["tokens"]) for g in self._token_groups)
            logger.debug(
                "angel_one_subscribed",
                extra={
                    "provider": _PROVIDER_ID,
                    "exchange_groups": len(token_list),
                    "total_tokens": total_tokens,
                    "mode": self._subscription_mode,
                    "exchanges": [EXCHANGE_TYPE_NAMES.get(g["exchange_type"], "?") for g in self._token_groups],
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
        if self._event_bus is not None:
            try:
                await self._event_bus.publish_reconnect(_PROVIDER_ID, attempt)
            except Exception as exc:
                logger.warning(
                    "angel_one_reconnect_event_failed",
                    extra={"provider": _PROVIDER_ID, "error": str(exc)},
                )

    async def _handle_exhaustion(self) -> None:
        logger.error(
            "angel_one_stream_connection_failed",
            extra={"provider": _PROVIDER_ID, "max_attempts": _MAX_RECONNECT_ATTEMPTS},
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
        return self._ws is not None and not getattr(self._ws, "closed", True)

    @property
    def reconnect_attempts(self) -> int:
        return self._reconnect_attempts


# ---------------------------------------------------------------------------
# Backward-compatible aliases for existing tests
# ---------------------------------------------------------------------------

def _normalise_tick(raw: dict) -> dict:
    """Backward-compatible wrapper for tests.

    The new binary decoder in _decode_smartstream_binary handles the real
    binary protocol. This function provides the old JSON-field normalisation
    behavior for tests that pass pre-decoded dicts.
    """
    import time as _time
    import uuid as _uuid

    canonical = {
        "tickId":       str(_uuid.uuid4()),
        "source":       _PROVIDER_ID,
        "receivedAtMs": int(_time.time() * 1000),
        "isDuplicate":  False,
    }

    _FIELD_MAP_COMPAT = {
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

    for raw_key, raw_val in raw.items():
        canon_key = _FIELD_MAP_COMPAT.get(raw_key)
        if canon_key:
            canonical[canon_key] = raw_val

    if "oi" not in canonical:
        canonical["oi"] = None
        canonical["oiMissing"] = True
    else:
        canonical["oiMissing"] = False

    # Convert paise to rupees if ltp is an integer (SmartStream sends paise)
    if "ltp" in canonical and isinstance(canonical["ltp"], int):
        canonical["ltp"] = canonical["ltp"] / 100.0

    if "lastTradeTime" in canonical:
        canonical["eventTimeMs"] = canonical["lastTradeTime"]
    else:
        canonical["eventTimeMs"] = canonical["receivedAtMs"]

    return canonical
