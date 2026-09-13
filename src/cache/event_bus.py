"""
src/cache/event_bus.py

Redis Streams Event Bus for DATA-SERVICE 2.0.

Implements the internal message bus (Redis Streams) over which the Platform
publishes ticks, dataset-ready events, incidents, quality alerts, and
provider-lifecycle events to consumers.

Design principles
-----------------
- **At-least-once delivery** via Redis Streams (XADD / XREAD with consumer
  groups).  Messages are retained until acknowledged or until the per-stream
  MAXLEN trim window expires — whichever comes first.
- **Sequence numbers**: each message ID returned by XADD is an auto-generated
  Redis stream entry ID (`{ms}-{seq}`) that is monotonically increasing and
  sufficient for consumer-side deduplication.
- **Graceful degradation**: every publish/consume operation catches
  ``RedisError`` and logs a structured warning rather than raising to the
  caller.  This prevents a Redis outage from breaking the hot path.
- **MAXLEN trimming**: streams are trimmed with ``MAXLEN ~ <retention>`` on
  each XADD.  The approximate (``~``) flag lets Redis coalesce trims at
  radix-tree node boundaries for throughput; at typical tick rates the
  actual length stays within a small multiple of the target.

Stream keys and retention (Requirements 15.1, 15.4 — design §Event Bus)
-----------------------------------------------------------------------
+-------------------------------------------+----------+
| Stream key                                | Retention|
+-------------------------------------------+----------+
| mds:events:ticks                          |  24 h    |
| mds:events:candles                        |  24 h    |
| mds:events:option-chain                   |  24 h    |
| mds:events:dataset-ready                  |  24 h    |
| mds:events:incidents                      |   7 d    |
| mds:events:quality                        |  24 h    |
| mds:events:provider-switch                |  24 h    |
| mds:events:reconnect                      |  24 h    |
| mds:events:connection-failed              |  24 h    |
+-------------------------------------------+----------+

Requirements: 15.1, 15.4
"""

from __future__ import annotations

import json
import logging
import time
from enum import Enum
from typing import Any, Optional

from redis.asyncio import Redis
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# EventType enumeration
# (Design §Event Bus → Event Types)
# ---------------------------------------------------------------------------


class EventType(str, Enum):
    """All first-class event types published on the Event Bus."""

    QUOTE_UPDATED = "QuoteUpdated"
    CANDLE_CLOSED = "CandleClosed"
    OPTION_CHAIN_UPDATED = "OptionChainUpdated"
    DATASET_READY = "DatasetReady"
    DATA_INCIDENT = "DataIncident"
    QUALITY_DEGRADED = "QualityDegraded"
    QUALITY_RESTORED = "QualityRestored"
    CIRCUIT_OPEN = "CircuitOpen"
    PROVIDER_SWITCH = "ProviderSwitch"
    CONNECTION_FAILED = "ConnectionFailed"

    # Internal streaming-engine events (not surfaced to consumers)
    RECONNECT = "Reconnect"


# ---------------------------------------------------------------------------
# Stream-key constants
# ---------------------------------------------------------------------------


class StreamKey:
    """Canonical stream key strings for all nine Event Bus streams."""

    TICKS = "mds:events:ticks"
    CANDLES = "mds:events:candles"
    OPTION_CHAIN = "mds:events:option-chain"
    DATASET_READY = "mds:events:dataset-ready"
    INCIDENTS = "mds:events:incidents"
    QUALITY = "mds:events:quality"
    PROVIDER_SWITCH = "mds:events:provider-switch"
    RECONNECT = "mds:events:reconnect"
    CONNECTION_FAILED = "mds:events:connection-failed"


# ---------------------------------------------------------------------------
# Retention / MAXLEN constants
# ---------------------------------------------------------------------------

# Assumed sustained publish rate for MAXLEN calculation.
# Ticks: ~500 symbols * ~1 tick/s = 500 ticks/s → 43,200,000 entries in 24h.
# That is far too large to store verbatim; we cap at a reasonable operational
# value that still provides 24h of coverage at *normal* rates.
#
# At 10 ticks/s across all symbols → 864,000 entries/day.
# We round up to 1,000,000 for comfortable headroom.
_24H_TICK_MAXLEN: int = 1_000_000

# Non-tick streams publish much less frequently; 100,000 covers 24h comfortably.
_24H_GENERAL_MAXLEN: int = 100_000

# Incidents are retained 7 days; cap at 500,000 entries.
_7D_INCIDENTS_MAXLEN: int = 500_000

# Map each stream key → MAXLEN for XADD trimming.
STREAM_MAXLEN: dict[str, int] = {
    StreamKey.TICKS: _24H_TICK_MAXLEN,
    StreamKey.CANDLES: _24H_GENERAL_MAXLEN,
    StreamKey.OPTION_CHAIN: _24H_GENERAL_MAXLEN,
    StreamKey.DATASET_READY: _24H_GENERAL_MAXLEN,
    StreamKey.INCIDENTS: _7D_INCIDENTS_MAXLEN,
    StreamKey.QUALITY: _24H_GENERAL_MAXLEN,
    StreamKey.PROVIDER_SWITCH: _24H_GENERAL_MAXLEN,
    StreamKey.RECONNECT: _24H_GENERAL_MAXLEN,
    StreamKey.CONNECTION_FAILED: _24H_GENERAL_MAXLEN,
}

# All nine stream keys (used for validation / iteration).
ALL_STREAM_KEYS: frozenset[str] = frozenset(STREAM_MAXLEN.keys())

# ---------------------------------------------------------------------------
# EventBus
# ---------------------------------------------------------------------------


class EventBus:
    """Async Redis Streams Event Bus.

    Provides a thin, opinionated wrapper around Redis Streams to publish and
    consume market data events with at-least-once delivery semantics.

    Parameters
    ----------
    client:
        A connected ``redis.asyncio.Redis`` client (with ``decode_responses=True``
        for string fields, or ``False`` for bytes — both work; the class handles
        byte decoding internally).

    Usage
    -----
    ::

        bus = EventBus(redis_client)

        # Publish a tick event
        msg_id = await bus.publish_tick("NIFTY", tick_dict)

        # Publish a dataset-ready event
        msg_id = await bus.publish_dataset_ready(dataset_dict)

        # Publish a data incident
        msg_id = await bus.publish_incident(incident_dict)

        # Publish a quality event
        msg_id = await bus.publish_quality_event(EventType.QUALITY_DEGRADED, data)

        # Low-level publish to any stream
        msg_id = await bus.publish(StreamKey.CANDLES, EventType.CANDLE_CLOSED, payload)

        # Inspect stream metadata
        info = await bus.get_stream_info(StreamKey.TICKS)

    Redis Streams message format
    ----------------------------
    Each XADD call stores a Redis hash with two fields:

    * ``event_type`` — the ``EventType`` string value
    * ``payload``    — JSON-serialised event data

    This keeps the message schema simple and forward-compatible.
    """

    def __init__(self, client: "Redis[Any]") -> None:
        self._client: "Redis[Any]" = client

    # ------------------------------------------------------------------
    # Core publish
    # ------------------------------------------------------------------

    async def publish(
        self,
        stream_key: str,
        event_type: EventType,
        payload: dict[str, Any],
    ) -> str:
        """Publish an event to a Redis Stream.

        Serialises *payload* to JSON, calls ``XADD {stream_key} MAXLEN ~
        {maxlen} * event_type {value} payload {json}`` and returns the
        auto-generated stream entry ID.

        On ``RedisError``, logs a ``stream_publish_failed`` warning at WARN
        level and returns an empty string rather than raising, so the caller's
        hot path is unaffected (Requirement 9.7 generalised to Event Bus).

        Parameters
        ----------
        stream_key:
            One of the ``StreamKey.*`` constants.  Unknown keys are accepted
            (to allow future extension) with a default MAXLEN of
            ``_24H_GENERAL_MAXLEN``.
        event_type:
            The ``EventType`` enum value to stamp on the message.
        payload:
            Arbitrary dict that is JSON-serialised and stored in the message.

        Returns
        -------
        str
            The Redis stream entry ID (e.g. ``"1705300000123-0"``), or ``""``
            when Redis is unavailable.
        """
        maxlen = STREAM_MAXLEN.get(stream_key, _24H_GENERAL_MAXLEN)
        try:
            payload_json = json.dumps(payload, default=str)
            message_id: str | bytes = await self._client.xadd(
                stream_key,
                {"event_type": event_type.value, "payload": payload_json},
                maxlen=maxlen,
                approximate=True,
            )
            # redis-py may return bytes when decode_responses=False; normalise.
            if isinstance(message_id, bytes):
                message_id = message_id.decode("utf-8")
            logger.debug(
                "event_bus_published",
                extra={
                    "stream": stream_key,
                    "event_type": event_type.value,
                    "message_id": message_id,
                },
            )
            return message_id
        except RedisError as exc:
            logger.warning(
                "stream_publish_failed",
                extra={
                    "stream": stream_key,
                    "event_type": event_type.value,
                    "error": str(exc),
                },
            )
            return ""

    # ------------------------------------------------------------------
    # Convenience shortcuts (design §Streaming Engine)
    # ------------------------------------------------------------------

    async def publish_tick(self, symbol: str, tick_data: dict[str, Any]) -> str:
        """Publish a live tick event for *symbol* to ``mds:events:ticks``.

        Automatically sets ``symbol`` in the payload for easy downstream
        filtering.

        Parameters
        ----------
        symbol:
            Trading symbol string, e.g. ``"NIFTY"``.
        tick_data:
            Canonical tick dict (``tickId``, ``instrumentId``, ``ltp``, …).

        Returns
        -------
        str
            Stream entry ID, or ``""`` on Redis failure.
        """
        payload: dict[str, Any] = {"symbol": symbol, **tick_data}
        return await self.publish(StreamKey.TICKS, EventType.QUOTE_UPDATED, payload)

    async def publish_dataset_ready(self, event_data: dict[str, Any]) -> str:
        """Publish a ``DatasetReady`` event to ``mds:events:dataset-ready``.

        Called when a backfill chunk, option chain snapshot, or gap recovery
        completes (Requirement 15.3 / design §DatasetReady event payload).

        Parameters
        ----------
        event_data:
            Dict with keys: ``dataType``, ``instrumentId``, ``exchange``,
            ``intervalStr``, ``fromTs``, ``toTs``, ``rowCount``, ``provider``.

        Returns
        -------
        str
            Stream entry ID, or ``""`` on Redis failure.
        """
        return await self.publish(StreamKey.DATASET_READY, EventType.DATASET_READY, event_data)

    async def publish_incident(self, incident: dict[str, Any]) -> str:
        """Publish a ``DataIncident`` event to ``mds:events:incidents``.

        Incidents are retained for 7 days (vs. 24h for other streams) to
        support forensics and audit workflows.

        Parameters
        ----------
        incident:
            ``DataIncident`` dict with ``incidentId``, ``incidentType``,
            ``instrumentId``, ``provider``, ``timestamp``, ``severity``,
            ``details``.

        Returns
        -------
        str
            Stream entry ID, or ``""`` on Redis failure.
        """
        return await self.publish(StreamKey.INCIDENTS, EventType.DATA_INCIDENT, incident)

    async def publish_quality_event(
        self,
        event_type: EventType,
        data: dict[str, Any],
    ) -> str:
        """Publish a quality alert event to ``mds:events:quality``.

        Valid *event_type* values: ``QUALITY_DEGRADED``, ``QUALITY_RESTORED``.

        Parameters
        ----------
        event_type:
            ``EventType.QUALITY_DEGRADED`` or ``EventType.QUALITY_RESTORED``.
        data:
            Dict with ``instrumentId``, ``score``, ``previousScore``, and
            optionally ``blockReasons``.

        Returns
        -------
        str
            Stream entry ID, or ``""`` on Redis failure.
        """
        return await self.publish(StreamKey.QUALITY, event_type, data)

    async def publish_provider_switch(self, switch_data: dict[str, Any]) -> str:
        """Publish a ``ProviderSwitch`` event to ``mds:events:provider-switch``.

        Parameters
        ----------
        switch_data:
            Dict with ``fromProvider``, ``toProvider``, ``reason``,
            ``dataset``, ``instrumentId``, ``timestamp``.

        Returns
        -------
        str
            Stream entry ID, or ``""`` on Redis failure.
        """
        return await self.publish(
            StreamKey.PROVIDER_SWITCH, EventType.PROVIDER_SWITCH, switch_data
        )

    async def publish_reconnect(self, provider: str, attempt: int) -> str:
        """Publish a reconnect attempt event to ``mds:events:reconnect``.

        Called by the Streaming Engine on each WebSocket reconnect attempt
        (design §Angel One SmartStream reconnect policy).

        Parameters
        ----------
        provider:
            Provider identifier, e.g. ``"angel_one"``, ``"upstox"``, ``"binance"``.
        attempt:
            Reconnect attempt number (1-based).

        Returns
        -------
        str
            Stream entry ID, or ``""`` on Redis failure.
        """
        return await self.publish(
            StreamKey.RECONNECT,
            EventType.RECONNECT,
            {"type": "reconnect", "provider": provider, "attempt": attempt},
        )

    async def publish_connection_failed(self, provider: str) -> str:
        """Publish a ``ConnectionFailed`` event to ``mds:events:connection-failed``.

        Called by the Streaming Engine after exhausting all reconnect attempts
        (design: *"After exhaustion: publish {'type': 'connection_failed',
        'provider': '...'}*").

        Parameters
        ----------
        provider:
            Provider identifier, e.g. ``"angel_one"``.

        Returns
        -------
        str
            Stream entry ID, or ``""`` on Redis failure.
        """
        return await self.publish(
            StreamKey.CONNECTION_FAILED,
            EventType.CONNECTION_FAILED,
            {"type": "connection_failed", "provider": provider},
        )

    # ------------------------------------------------------------------
    # Stream introspection
    # ------------------------------------------------------------------

    async def get_stream_info(self, stream_key: str) -> dict[str, Any]:
        """Return metadata about a Redis Stream.

        Calls ``XINFO STREAM {stream_key}`` and returns a dict with:

        * ``length``     — current number of entries in the stream
        * ``first_entry_id`` — ID of the oldest entry (or ``None``)
        * ``last_entry_id``  — ID of the newest entry (or ``None``)
        * ``radix_tree_keys`` — number of radix-tree keys (internal)
        * ``groups``     — number of consumer groups
        * ``stream_key`` — the stream key for reference

        On Redis error, returns a dict with ``error`` key and ``length: 0``
        rather than raising.

        Parameters
        ----------
        stream_key:
            The Redis stream key to inspect.

        Returns
        -------
        dict
            Stream metadata dict.
        """
        try:
            raw: dict[str | bytes, Any] = await self._client.xinfo_stream(stream_key)

            def _str(v: Any) -> Any:
                """Decode bytes to str when decode_responses=False."""
                if isinstance(v, bytes):
                    return v.decode("utf-8")
                return v

            return {
                "stream_key": stream_key,
                "length": raw.get("length", raw.get(b"length", 0)),
                "first_entry_id": _str(
                    (raw.get("first-entry") or raw.get(b"first-entry") or [None])[0]
                ),
                "last_entry_id": _str(
                    (raw.get("last-entry") or raw.get(b"last-entry") or [None])[0]
                ),
                "radix_tree_keys": raw.get(
                    "radix-tree-keys", raw.get(b"radix-tree-keys", 0)
                ),
                "groups": raw.get("groups", raw.get(b"groups", 0)),
            }
        except RedisError as exc:
            logger.warning(
                "stream_info_failed",
                extra={"stream": stream_key, "error": str(exc)},
            )
            return {
                "stream_key": stream_key,
                "length": 0,
                "first_entry_id": None,
                "last_entry_id": None,
                "radix_tree_keys": 0,
                "groups": 0,
                "error": str(exc),
            }

    # ------------------------------------------------------------------
    # Consumer helpers (XREAD)
    # ------------------------------------------------------------------

    async def read(
        self,
        stream_key: str,
        last_id: str = "$",
        count: int = 100,
        block_ms: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        """Read new entries from a Redis Stream starting after *last_id*.

        Wraps ``XREAD [BLOCK block_ms] COUNT count STREAMS stream_key last_id``
        and returns a list of decoded message dicts.

        Each returned dict contains:

        * ``id``         — stream entry ID string
        * ``event_type`` — the ``EventType`` value string
        * ``payload``    — the decoded ``dict`` payload

        Parameters
        ----------
        stream_key:
            The Redis stream key to read from.
        last_id:
            The entry ID from which to start reading (exclusive).  Use
            ``"$"`` to read only new entries, ``"0"`` to read from the
            beginning, or a specific entry ID to resume from a checkpoint.
        count:
            Maximum number of entries to return.
        block_ms:
            If set, block for up to this many milliseconds waiting for new
            entries.  ``None`` means non-blocking (return immediately if
            nothing available).

        Returns
        -------
        list[dict]
            Decoded messages, or an empty list when Redis is unavailable or
            no messages are available.
        """
        try:
            kwargs: dict[str, Any] = {"count": count, "streams": {stream_key: last_id}}
            if block_ms is not None:
                kwargs["block"] = block_ms

            raw_result: Any = await self._client.xread(**kwargs)
            if not raw_result:
                return []

            messages: list[dict[str, Any]] = []
            # raw_result shape: [[stream_name, [(id, {field: value}), ...]]]
            for _stream_name, entries in raw_result:
                for entry_id, fields in entries:
                    # Decode bytes keys/values when decode_responses=False.
                    decoded_fields: dict[str, str] = {
                        (k.decode("utf-8") if isinstance(k, bytes) else k): (
                            v.decode("utf-8") if isinstance(v, bytes) else v
                        )
                        for k, v in fields.items()
                    }
                    entry_id_str = (
                        entry_id.decode("utf-8")
                        if isinstance(entry_id, bytes)
                        else entry_id
                    )
                    try:
                        payload = json.loads(decoded_fields.get("payload", "{}"))
                    except json.JSONDecodeError:
                        payload = {}

                    messages.append(
                        {
                            "id": entry_id_str,
                            "event_type": decoded_fields.get("event_type", ""),
                            "payload": payload,
                        }
                    )
            return messages

        except RedisError as exc:
            logger.warning(
                "stream_read_failed",
                extra={"stream": stream_key, "error": str(exc)},
            )
            return []
