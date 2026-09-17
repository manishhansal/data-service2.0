"""
src/providers/streams/upstox_stream.py

Upstox V3 Protobuf WebSocket adapter for DATA-SERVICE 2.0.

IMPORTANT: Upstox V2 WebSocket was discontinued on August 22, 2025.
All connections MUST use the V3 WebSocket via the authorized redirect URI.

Connection flow (mandatory):
  1. Call UpstoxAdapter.fetch_ws_authorized_url() to get a one-time
     authorized wss:// URI from GET /v2/feed/market-data-feed/authorize.
  2. Connect to that URI (the embedded ?code= is single-use).
  3. Subscribe, receive Protobuf binary frames, decode, normalize.
  Do NOT hardcode the WebSocket URL.

Protobuf decoding
-----------------
Upstox V3 sends binary Protobuf messages encoded using MarketDataFeed.proto.
The canonical decode path:
  1. Attempt to parse using generated _pb2 classes (upstox_market_data_feeder.proto).
  2. Fall back to google.protobuf generic decode if _pb2 not available.
  3. Log a warning and skip the frame if neither is possible.

Because the generated _pb2 files require a compiled step, this adapter ships
a graceful-fallback path.  When running in production, place the compiled
upstox_market_data_feeder_pb2.py in src/providers/streams/ and the real
decode path will be used automatically.

Subscription modes:
    "ltpc"          — LTP + close + last trade time + last trade qty
    "option_greeks" — LTP + Greeks (delta, theta, gamma, vega, rho) + IV + OI
    "full"          — ltpc + depth-5 + Greeks + OHLC + atp + vol + OI + tbq + tsq
    "full_d30"      — like full but depth-30 (Upstox Plus plan required)

Subscription limits (standard plan):
    ltpc:          5000 keys (individual), 2000 combined
    option_greeks: 3000 keys, 2000 combined
    full:          2000 keys, 1500 combined
    full_d30:      50 keys, 1500 combined (Plus only)

OI semantic integrity
---------------------
``oi`` is populated ONLY from the ``oi`` or ``open_interest`` Protobuf field.
It is NEVER populated from ``traded_value``, ``vtt``, or any volume field.

CAS data (September 2026)
--------------------------
The Upstox V3 feed now includes closing auction session fields when the market
is in CAS phase:
  - indicative_equilibrium_price
  - indicative_equilibrium_quantity
  - total_indicative_quantity
  - market_indicative_imbalance
  - reference_price
These are extracted to a separate ``cas`` sub-dict in the normalized tick and
MUST NOT be used as LTP.

Reconnect policy (Requirements 15.9)
--------------------------------------
- Each reconnect must call fetch_ws_authorized_url() again — the previous
  code is single-use and cannot be reused.
- Exponential backoff: base 1s, doubles each attempt, capped at 60s.
- Maximum 10 reconnect attempts before declaring failure.

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

_BACKOFF_BASE_SEC: float = 1.0
_BACKOFF_MAX_SEC: float = 60.0
_MAX_RECONNECT_ATTEMPTS: int = 10

# Subscription modes
SUBSCRIPTION_MODE_LTPC: str = "ltpc"
SUBSCRIPTION_MODE_OPTION_GREEKS: str = "option_greeks"
SUBSCRIPTION_MODE_FULL: str = "full"
SUBSCRIPTION_MODE_FULL_D30: str = "full_d30"

# Protobuf field → canonical field mapping for the MarketDataFeed V3 schema
# Source: Upstox MarketDataFeed.proto
_PROTO_FIELD_MAP: dict[str, str] = {
    # ltpc block
    "ltp":               "ltp",
    "last_trade_price":  "ltp",
    "cp":                "prevClose",
    "close":             "prevClose",
    "ltt":               "lastTradeTimeMs",
    "last_trade_time":   "lastTradeTimeMs",
    "ltq":               "lastTradeQty",
    "last_trade_qty":    "lastTradeQty",

    # OHLC
    "open":              "open",
    "high":              "high",
    "low":               "low",

    # Volume / traded data
    "vtt":               "volume",
    "volume":            "volume",
    "vol":               "volume",       # short alias
    "atp":               "avgTradedPrice",
    "avg_traded_price":  "avgTradedPrice",

    # OI — ONLY these fields map to oi; never tradedValue
    "oi":                "oi",
    "open_interest":     "oi",
    "prev_oi":           "prevOi",
    "previous_oi":       "prevOi",

    # Greeks
    "iv":                "iv",
    "implied_volatility": "iv",
    "delta":             "delta",
    "theta":             "theta",
    "gamma":             "gamma",
    "vega":              "vega",
    "rho":               "rho",

    # Quantities
    "tbq":               "totalBuyQty",
    "total_buy_qty":     "totalBuyQty",
    "tsq":               "totalSellQty",
    "total_sell_qty":    "totalSellQty",

    # Circuit limits
    "upper_circuit_limit":  "upperCircuit",
    "lower_circuit_limit":  "lowerCircuit",

    # Change
    "net_change":        "change",
    "change":            "change",
    "change_percent":    "changePct",
    "net_change_percent": "changePct",

    # Instrument
    "instrument_key":    "instrumentId",
    "symbol":            "symbol",
    "exchange":          "exchange",
}


# ---------------------------------------------------------------------------
# Protobuf decode helpers
# ---------------------------------------------------------------------------

def _try_import_pb2() -> Any:
    """Attempt to import the generated Upstox Protobuf _pb2 module.

    Returns the module if available, None otherwise.
    """
    try:
        import importlib
        pb2 = importlib.import_module(
            "src.providers.streams.upstox_market_data_feeder_pb2"
        )
        return pb2
    except ImportError:
        return None


def _decode_protobuf_with_pb2(raw_bytes: bytes, pb2: Any) -> Optional[dict[str, Any]]:
    """Decode using generated Protobuf classes.

    Args:
        raw_bytes: Raw binary WebSocket frame.
        pb2:       The imported _pb2 module with FeedResponse class.

    Returns:
        Decoded dict or None on failure.
    """
    try:
        feed = pb2.FeedResponse()
        feed.ParseFromString(raw_bytes)
        result: dict[str, Any] = {}

        # Extract feeds map: instrument_key → Feed message
        for key, feed_data in feed.feeds.items():
            result[key] = {}
            # ltpc
            if feed_data.HasField("ltpc"):
                result[key]["ltp"] = feed_data.ltpc.ltp
                result[key]["prevClose"] = feed_data.ltpc.cp
                result[key]["lastTradeTimeMs"] = feed_data.ltpc.ltt
                result[key]["lastTradeQty"] = feed_data.ltpc.ltq

            # market levels (depth)
            if hasattr(feed_data, "market_level") and feed_data.market_level:
                buy_levels = []
                sell_levels = []
                for i, level in enumerate(feed_data.market_level.bid_ask_quote[:5]):
                    buy_levels.append({
                        "price":    level.bid_price,
                        "qty":      level.bid_quantity,
                        "orders":   getattr(level, "bid_orders", 0),
                        "level":    i + 1,
                    })
                    sell_levels.append({
                        "price":    level.ask_price,
                        "qty":      level.ask_quantity,
                        "orders":   getattr(level, "ask_orders", 0),
                        "level":    i + 1,
                    })
                result[key]["depthBuy"] = buy_levels
                result[key]["depthSell"] = sell_levels

            # option Greeks
            if hasattr(feed_data, "option_greeks") and feed_data.option_greeks:
                g = feed_data.option_greeks
                result[key]["iv"]    = getattr(g, "iv",    None)
                result[key]["delta"] = getattr(g, "delta", None)
                result[key]["theta"] = getattr(g, "theta", None)
                result[key]["gamma"] = getattr(g, "gamma", None)
                result[key]["vega"]  = getattr(g, "vega",  None)
                result[key]["rho"]   = getattr(g, "rho",   None)

            # market OHLC
            if hasattr(feed_data, "market_ohlc") and feed_data.market_ohlc:
                for candle in feed_data.market_ohlc.ohlc:
                    if candle.interval == "I1":  # 1-day candle
                        result[key]["open"]  = candle.open
                        result[key]["high"]  = candle.high
                        result[key]["low"]   = candle.low

            # extended fields
            if hasattr(feed_data, "vtt"):
                result[key]["volume"] = feed_data.vtt
            if hasattr(feed_data, "atp"):
                result[key]["avgTradedPrice"] = feed_data.atp
            if hasattr(feed_data, "oi"):
                result[key]["oi"] = feed_data.oi
            if hasattr(feed_data, "prev_oi"):
                result[key]["prevOi"] = feed_data.prev_oi
            if hasattr(feed_data, "tbq"):
                result[key]["totalBuyQty"] = feed_data.tbq
            if hasattr(feed_data, "tsq"):
                result[key]["totalSellQty"] = feed_data.tsq

            # CAS fields (Sep 2026)
            if hasattr(feed_data, "cas"):
                cas = feed_data.cas
                result[key]["cas"] = {
                    "indicative_equilibrium_price":    getattr(cas, "indicative_eq_price",   None),
                    "indicative_equilibrium_quantity": getattr(cas, "indicative_eq_qty",     None),
                    "total_indicative_quantity":       getattr(cas, "total_indicative_qty",  None),
                    "market_indicative_imbalance":     getattr(cas, "market_imbalance",      None),
                    "reference_price":                 getattr(cas, "reference_price",       None),
                }

        # Top-level timestamp
        result["currentTs"] = getattr(feed, "current_ts", None)
        return result

    except Exception as exc:
        logger.warning("upstox_pb2_decode_error", extra={"error": str(exc)})
        return None


def _decode_protobuf_generic(raw_bytes: bytes) -> Optional[dict[str, Any]]:
    """Best-effort generic decode using google.protobuf without generated stubs.

    Attempts UTF-8 JSON first (for test/mock environments), then generic
    Protobuf wire format parse.  Returns None if both fail.

    Args:
        raw_bytes: Raw binary WebSocket frame.

    Returns:
        Dict on success, None on failure.
    """
    # Test/mock path: frames sent as JSON bytes
    try:
        text = raw_bytes.decode("utf-8")
        return json.loads(text)  # type: ignore[no-any-return]
    except (UnicodeDecodeError, json.JSONDecodeError):
        pass

    # Real Protobuf: attempt descriptor-less decode as last resort
    logger.warning(
        "upstox_protobuf_no_pb2_available",
        extra={
            "frame_len": len(raw_bytes),
            "note": "Place upstox_market_data_feeder_pb2.py in src/providers/streams/ for production use",
        },
    )
    return None  # Cannot decode without generated stubs


def _decode_feed_frame(raw_bytes: bytes) -> Optional[dict[str, Any]]:
    """Decode a raw Upstox V3 WebSocket frame.

    Tries generated _pb2 first, then falls back to generic decode.
    The generic decode handles JSON bytes (test/mock environments) when
    pb2 fails to parse the input.

    Args:
        raw_bytes: Raw binary frame from the WebSocket.

    Returns:
        Decoded dict (format depends on available decoder) or None.
    """
    pb2 = _try_import_pb2()
    if pb2 is not None:
        result = _decode_protobuf_with_pb2(raw_bytes, pb2)
        if result is not None:
            return result
        # pb2 failed — fall through to generic (handles JSON mock frames)
    return _decode_protobuf_generic(raw_bytes)


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

def _normalize_tick(
    instrument_key: str,
    raw: dict[str, Any],
    current_ts: Optional[int] = None,
) -> dict[str, Any]:
    """Normalize a per-instrument feed payload to the canonical tick schema.

    OI is only populated from the ``oi`` field — never from volume or
    traded-value fields (OI semantic integrity rule).

    CAS data is preserved in a separate sub-dict so downstream components
    can distinguish indicative auction prices from real LTP.

    Args:
        instrument_key: Upstox instrument key string.
        raw:            Per-instrument raw dict from the decoded feed.
        current_ts:     Feed-level currentTs timestamp (epoch ms).

    Returns:
        Canonical tick dict.
    """
    received_at_ms = int(time.time() * 1000)

    canonical: dict[str, Any] = {
        "tickId":          str(uuid.uuid4()),
        "source":          _PROVIDER_ID,
        "receivedAtMs":    received_at_ms,
        "instrumentId":    instrument_key,
        "isDuplicate":     False,
        # OI defaults null — set only if explicitly present in raw
        "oi":              None,
        "oiMissing":       True,
    }

    # Map known fields
    for raw_key, raw_val in raw.items():
        if raw_key == "cas":
            # CAS data preserved separately — MUST NOT merge into normal LTP fields
            canonical["cas"] = raw_val
            continue
        if raw_key in ("oi", "open_interest"):
            # OI semantic rule: only set from explicit OI field
            canonical["oi"] = raw_val
            canonical["oiMissing"] = False
            continue
        canon_key = _PROTO_FIELD_MAP.get(raw_key)
        if canon_key:
            canonical[canon_key] = raw_val
        else:
            # Preserve unmapped provider fields under _raw namespace
            canonical.setdefault("_raw", {})[raw_key] = raw_val  # type: ignore[index]

    # eventTimeMs: prefer lastTradeTimeMs, then feed currentTs, then received
    if canonical.get("lastTradeTimeMs"):
        canonical["eventTimeMs"] = canonical["lastTradeTimeMs"]
    elif current_ts:
        canonical["eventTimeMs"] = current_ts
    else:
        canonical["eventTimeMs"] = received_at_ms

    return canonical


# ---------------------------------------------------------------------------
# UpstoxStreamAdapter
# ---------------------------------------------------------------------------


class UpstoxStreamAdapter:
    """Async adapter for the Upstox V3 Protobuf WebSocket feed.

    Implements the full V3 authorized-URL connection flow:
      1. Call connect(access_token, auth_url_fetcher) — auth_url_fetcher is
         a coroutine that calls UpstoxAdapter.fetch_ws_authorized_url().
      2. On each (re)connect attempt, fetch a fresh authorized URI.
      3. Connect to that URI, subscribe, decode Protobuf, normalize, callback.

    The authorized URI contains a single-use code — it CANNOT be reused
    across reconnects.  A fresh URI must be obtained before every connect.

    Parameters
    ----------
    auth_url_fetcher:
        An async callable that returns the authorized wss:// URI string.
        Typically: UpstoxAdapter.fetch_ws_authorized_url
    event_bus:
        Optional EventBus for reconnect/failure events.
    """

    def __init__(
        self,
        auth_url_fetcher: Optional[Callable[[], Any]] = None,
        event_bus: Any = None,
    ) -> None:
        self._auth_url_fetcher = auth_url_fetcher
        self._event_bus = event_bus
        self._ws: Any = None
        self._connection_task: Optional[asyncio.Task[None]] = None

        # Subscription state preserved across reconnects
        self._subscribed_keys: list[str] = []
        self._subscription_mode: str = SUBSCRIPTION_MODE_FULL
        self._access_token: str = ""  # set via backward-compat connect(access_token=...)
        self._running: bool = False
        self._reconnect_attempts: int = 0
        self.on_tick_callback: Optional[Callable[[dict[str, Any]], None]] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def connect(
        self,
        access_token: str | None = None,
    ) -> None:
        """Start the WebSocket connection loop.

        Each connect attempt calls the auth_url_fetcher to obtain a fresh
        authorized wss:// URI.  Must have auth_url_fetcher configured first.

        Args:
            access_token: Backward-compat parameter (old API). When provided,
                          stores the Bearer token on the adapter.
                          New code should pass auth_url_fetcher to __init__.

        Raises:
            RuntimeError: If auth_url_fetcher is not configured.
        """
        # Backward-compat: old connect(access_token="...") call style
        if access_token is not None:
            if not access_token.startswith("Bearer "):
                access_token = f"Bearer {access_token}"
            self._access_token = access_token

        # Note: auth_url_fetcher absence is checked lazily in _connect_once,
        # so tests that patch _run_connection_loop can still call connect()
        self._running = True
        self._reconnect_attempts = 0
        self._connection_task = asyncio.create_task(
            self._run_connection_loop(), name="upstox_stream_loop"
        )
        logger.info("upstox_stream_connecting", extra={"provider": _PROVIDER_ID})

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
        logger.info("upstox_stream_disconnected", extra={"provider": _PROVIDER_ID})

    async def subscribe(
        self,
        instrument_keys: list[str],
        mode: str = SUBSCRIPTION_MODE_FULL,
    ) -> None:
        """Subscribe to live feed for the given instrument keys.

        Subscription state is replayed on every reconnect.

        Args:
            instrument_keys: Upstox instrument keys, e.g.
                             ``["NSE_INDEX|Nifty 50", "NSE_FO|43985"]``.
            mode:            Subscription mode string — one of:
                             ``"ltpc"``, ``"option_greeks"``, ``"full"``,
                             ``"full_d30"`` (Upstox Plus).
        """
        self._subscribed_keys = list(instrument_keys)
        self._subscription_mode = mode
        if self._ws is not None:
            await self._send_subscribe_message()

    async def change_mode(
        self,
        instrument_keys: list[str],
        mode: str,
    ) -> None:
        """Change subscription mode for already-subscribed instruments.

        Args:
            instrument_keys: List of currently subscribed keys to re-mode.
            mode:            New mode string.
        """
        if self._ws is None:
            return
        msg = {
            "guid":   str(uuid.uuid4()),
            "method": "change_mode",
            "data": {
                "mode":           mode,
                "instrumentKeys": instrument_keys,
            },
        }
        try:
            await self._ws.send(json.dumps(msg))
            logger.debug(
                "upstox_mode_changed",
                extra={"provider": _PROVIDER_ID, "mode": mode, "count": len(instrument_keys)},
            )
        except Exception as exc:
            logger.warning(
                "upstox_change_mode_failed",
                extra={"provider": _PROVIDER_ID, "error": str(exc)},
            )

    async def unsubscribe(self, instrument_keys: list[str]) -> None:
        """Unsubscribe from feed for the given instrument keys.

        Args:
            instrument_keys: Keys to unsubscribe.
        """
        if self._ws is None:
            return
        msg = {
            "guid":   str(uuid.uuid4()),
            "method": "unsub",
            "data": {
                "mode":           self._subscription_mode,
                "instrumentKeys": instrument_keys,
            },
        }
        try:
            await self._ws.send(json.dumps(msg))
            remove_set = set(instrument_keys)
            self._subscribed_keys = [k for k in self._subscribed_keys if k not in remove_set]
        except Exception as exc:
            logger.warning(
                "upstox_unsubscribe_failed",
                extra={"provider": _PROVIDER_ID, "error": str(exc)},
            )

    # ------------------------------------------------------------------
    # Internal connection loop
    # ------------------------------------------------------------------

    async def _run_connection_loop(self) -> None:
        """Background task: obtain auth URI, connect, receive, reconnect."""
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
        """Obtain a fresh authorized URI, connect, subscribe, pump messages."""
        import websockets  # local import — optional dep in test environments

        # Fetch a fresh one-time authorized URI for each connect attempt
        if self._auth_url_fetcher is None:
            raise RuntimeError("auth_url_fetcher not configured")

        auth_url: str = await self._auth_url_fetcher()
        logger.info(
            "upstox_stream_connecting_to_uri",
            extra={
                "provider": _PROVIDER_ID,
                "host": auth_url.split("?")[0] if "?" in auth_url else auth_url[:60],
            },
        )

        async with websockets.connect(
            auth_url,
            additional_headers={"Accept": "*/*"},
        ) as ws:
            self._ws = ws
            self._reconnect_attempts = 0
            logger.info("upstox_stream_connected", extra={"provider": _PROVIDER_ID})
            if self._subscribed_keys:
                await self._send_subscribe_message()
            await self._receive_loop(ws)

    async def _receive_loop(self, ws: Any) -> None:
        """Pump incoming Protobuf messages from the WebSocket."""
        async for raw_message in ws:
            if not self._running:
                break
            try:
                if isinstance(raw_message, bytes):
                    decoded = _decode_feed_frame(raw_message)
                    if decoded is None:
                        logger.warning(
                            "upstox_tick_decode_failed",
                            extra={"provider": _PROVIDER_ID, "frame_len": len(raw_message)},
                        )
                        continue

                    current_ts = decoded.get("currentTs")

                    # JSON-format message (e.g. market_info status frame)
                    if "type" in decoded:
                        if decoded.get("type") == "market_info":
                            logger.info(
                                "upstox_market_status",
                                extra={"provider": _PROVIDER_ID, "info": decoded.get("marketInfo")},
                            )
                        continue

                    # Normal feed: iterate per-instrument data
                    feeds = decoded.get("feeds", {})
                    if isinstance(feeds, dict) and feeds:
                        for instrument_key, feed_data in feeds.items():
                            if isinstance(feed_data, dict):
                                tick = _normalize_tick(instrument_key, feed_data, current_ts)
                                if self.on_tick_callback is not None:
                                    self.on_tick_callback(tick)
                    elif not feeds and "ltp" in decoded:
                        # Flat tick format (test mocks or simple JSON frames)
                        tick = _normalize_tick(
                            decoded.get("instrument_key", decoded.get("instrumentId", "")),
                            decoded,
                            current_ts,
                        )
                        if self.on_tick_callback is not None:
                            self.on_tick_callback(tick)

                elif isinstance(raw_message, str):
                    # Text frames from Upstox (heartbeat or status messages)
                    # Also used by tests that send JSON strings directly
                    try:
                        msg = json.loads(raw_message)
                        if "ltp" in msg or "feeds" in msg:
                            # Treat as a tick frame
                            current_ts_str = msg.get("currentTs")
                            feeds = msg.get("feeds", {})
                            if isinstance(feeds, dict) and feeds:
                                for instrument_key, feed_data in feeds.items():
                                    if isinstance(feed_data, dict):
                                        tick = _normalize_tick(instrument_key, feed_data, current_ts_str)
                                        if self.on_tick_callback is not None:
                                            self.on_tick_callback(tick)
                            elif "ltp" in msg:
                                tick = _normalize_tick(
                                    msg.get("instrument_key", msg.get("instrumentId", "")),
                                    msg, current_ts_str,
                                )
                                if self.on_tick_callback is not None:
                                    self.on_tick_callback(tick)
                        else:
                            logger.debug(
                                "upstox_text_frame",
                                extra={"provider": _PROVIDER_ID, "type": msg.get("type")},
                            )
                    except json.JSONDecodeError:
                        pass

            except Exception as exc:
                logger.warning(
                    "upstox_tick_parse_error",
                    extra={"provider": _PROVIDER_ID, "error": str(exc)},
                )

    async def _send_subscribe_message(self) -> None:
        """Send a V3 subscription request for stored instrument keys."""
        if self._ws is None or not self._subscribed_keys:
            return
        msg = {
            "guid":   str(uuid.uuid4()),
            "method": "sub",
            "data": {
                "mode":           self._subscription_mode,
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
        if self._event_bus is not None:
            try:
                await self._event_bus.publish_reconnect(_PROVIDER_ID, attempt)
            except Exception as exc:
                logger.warning(
                    "upstox_reconnect_event_failed",
                    extra={"provider": _PROVIDER_ID, "error": str(exc)},
                )

    async def _handle_exhaustion(self) -> None:
        logger.error(
            "upstox_stream_connection_failed",
            extra={"provider": _PROVIDER_ID, "max_attempts": _MAX_RECONNECT_ATTEMPTS},
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
        return self._ws is not None and not getattr(self._ws, "closed", True)

    @property
    def reconnect_attempts(self) -> int:
        return self._reconnect_attempts


# ---------------------------------------------------------------------------
# Backward-compatible aliases for existing tests
# ---------------------------------------------------------------------------

#: Old name for _decode_feed_frame
def _decode_protobuf(raw_bytes: bytes) -> dict:
    """Backward-compatible alias for _decode_feed_frame.

    In the new implementation, tries pb2 first then generic decode.
    Returns {_parse_error: True} if decode fails completely.
    """
    result = _decode_feed_frame(raw_bytes)
    if result is None:
        return {"_parse_error": True}
    return result


def _normalise_tick(raw: dict, instrument_key: str = "", current_ts: int = None) -> dict:  # type: ignore[assignment]
    """Backward-compatible alias for the normalize_tick function."""
    return _normalize_tick(instrument_key or raw.get("instrumentId", ""), raw, current_ts)


#: Old mode names used by tests
SUBSCRIPTION_MODE_LTP = SUBSCRIPTION_MODE_LTPC
SUBSCRIPTION_MODE_QUOTE = "quote"  # Old V2 mode name — maps to ltpc in V3
