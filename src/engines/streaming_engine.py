"""
src/engines/streaming_engine.py

Streaming Engine — tick publisher with deduplication.

Responsibilities (Task 8.5, Requirements 3.5, 3.6, 15.2, 8.7):
  - ``publish_tick(tick)`` — deduplicate incoming ticks with a 24-hour rolling
    window, set ``isDuplicate`` accordingly, and publish every tick (including
    duplicates) to the Event Bus.  Duplicate ticks are never silently dropped
    so that the audit trail is preserved.
  - ``publish_dataset_ready(event_data)`` — publish a DatasetReady event when
    a backfill chunk, option chain snapshot, or gap recovery completes.
  - ``get_stream_status()`` — return current streaming status metrics.

Deduplication (Requirements 3.6, 17.4 — Property 4):
  Deduplication ID formula: ``SHA-256(instrumentId:eventTimeMs:source:ltp:volume)[:32]``
  Computed by ``compute_dedup_hash`` from ``src.core.validators.dedup``.
  A duplicate is any tick whose hash matches one seen within the preceding
  24-hour rolling window.  Duplicate ticks are published with
  ``isDuplicate: True`` rather than dropped, so downstream consumers can
  maintain audit trails via the Event Bus sequence numbers.

Publish latency target: ≤ 200ms at p99 from ``receivedAtMs`` to Event Bus
delivery (Requirement 3.5 / 15.2).

Channel pattern: ``mds:ticks:{symbol}``
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

from src.cache.event_bus import EventBus
from src.core.validators.dedup import DedupStore, compute_dedup_hash

logger = logging.getLogger(__name__)

__all__ = ["StreamingEngine"]


class StreamingEngine:
    """Tick publisher with 24-hour rolling-window deduplication.

    Parameters
    ----------
    event_bus:
        An initialised :class:`~src.cache.event_bus.EventBus` instance used
        to publish ticks and dataset-ready events.  When ``None``, publish
        operations will still track internal state but will not emit anything
        to Redis (useful for unit testing without a live Redis connection when
        an explicit ``None`` is passed and all EventBus calls are mocked).
    dedup_store:
        An existing :class:`~src.core.validators.dedup.DedupStore` instance.
        When ``None``, a fresh in-process store is created.

    Usage
    -----
    ::

        engine = StreamingEngine(event_bus=bus)
        msg_id = await engine.publish_tick(tick_dict)
        await engine.publish_dataset_ready(event_data)
        status = engine.get_stream_status()
    """

    def __init__(
        self,
        event_bus: Optional[EventBus] = None,
        dedup_store: Optional[DedupStore] = None,
    ) -> None:
        self._event_bus: Optional[EventBus] = event_bus
        self._dedup_store: DedupStore = dedup_store if dedup_store is not None else DedupStore()

        # Counters (in-process; reset on restart)
        self._ticks_published: int = 0
        self._duplicate_count: int = 0
        self._last_published_at: Optional[str] = None

        # Validation failure tracking keyed by failure type.
        self._validation_failures: dict[str, int] = {}

        # Subscribed symbols (managed by WebSocket layer; tracked here for
        # the status response aggregation).
        self._subscribed_symbols: set[str] = set()

        # Broker connection states (updated by respective stream adapters).
        self._broker_connections: dict[str, bool] = {
            "angelOne": False,
            "upstox": False,
            "binance": False,
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def publish_tick(self, tick: dict[str, Any]) -> str:
        """Publish a normalised tick to the Event Bus.

        Computes the deduplication hash from the tick's five-tuple fields,
        checks the 24-hour rolling window, sets ``isDuplicate`` on the tick,
        then publishes to the Event Bus regardless of duplicate status.

        Duplicate ticks are always published (with ``isDuplicate: True``) to
        preserve the audit trail (Requirement 3.6).

        Parameters
        ----------
        tick:
            Canonical tick dict.  Must contain at minimum:
            ``instrumentId``, ``eventTimeMs``, ``source``, ``ltp``,
            ``volume``, ``symbol``.

        Returns
        -------
        str
            Stream entry ID returned by the Event Bus, or ``""`` when the
            Event Bus is unavailable.
        """
        # ── 1. Extract fields required for dedup hash ──────────────────
        instrument_id: str = str(tick.get("instrumentId", ""))
        event_time_ms: int = int(tick.get("eventTimeMs", 0))
        source: str = str(tick.get("source", ""))
        ltp: float = float(tick.get("ltp", 0.0))
        volume: int = int(tick.get("volume", 0))
        symbol: str = str(tick.get("symbol", ""))

        # ── 2. Compute dedup hash (Property 4 — deterministic) ─────────
        dedup_hash = compute_dedup_hash(
            instrument_id=instrument_id,
            event_time_ms=event_time_ms,
            source=source,
            ltp=ltp,
            volume=volume,
        )

        # ── 3. Check + record in rolling window ────────────────────────
        is_duplicate, _recorded = await self._dedup_store.check_and_record(
            dedup_hash=dedup_hash,
            window_hours=24,
        )

        # ── 4. Stamp isDuplicate on the tick dict ──────────────────────
        tick["isDuplicate"] = is_duplicate

        if is_duplicate:
            self._duplicate_count += 1
            logger.debug(
                "tick_duplicate",
                extra={
                    "instrumentId": instrument_id,
                    "eventTimeMs": event_time_ms,
                    "dedupHash": dedup_hash,
                },
            )
        else:
            logger.debug(
                "tick_unique",
                extra={
                    "instrumentId": instrument_id,
                    "eventTimeMs": event_time_ms,
                    "dedupHash": dedup_hash,
                },
            )

        # ── 5. Publish to Event Bus ────────────────────────────────────
        message_id = ""
        if self._event_bus is not None:
            message_id = await self._event_bus.publish_tick(symbol, tick)

        # ── 6. Update counters and last-published timestamp ────────────
        self._ticks_published += 1
        self._last_published_at = datetime.now(tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.%f"
        )[:-3] + "Z"

        # Track symbol subscription (add symbol as active if seen in a tick)
        if symbol:
            self._subscribed_symbols.add(symbol)

        return message_id

    async def publish_dataset_ready(self, event_data: dict[str, Any]) -> str:
        """Publish a DatasetReady event to the Event Bus.

        Called when a backfill chunk, option chain snapshot, or gap recovery
        completes (Requirement 15.3 / Task 8.7).

        Parameters
        ----------
        event_data:
            Dict with keys: ``dataType``, ``instrumentId``, ``exchange``,
            ``intervalStr``, ``fromTs``, ``toTs``, ``rowCount``, ``provider``.

        Returns
        -------
        str
            Stream entry ID, or ``""`` when Event Bus is unavailable.
        """
        if self._event_bus is None:
            return ""

        return await self._event_bus.publish_dataset_ready(event_data)

    async def publish_dataset_ready_simple(
        self,
        *,
        market: str,
        symbol: str,
        interval: str,
        date: str,
        record_count: int,
    ) -> str:
        """Convenience wrapper that accepts individual fields instead of a dict.

        Publishes a ``DatasetReady`` event to ``mds:events:dataset-ready`` with
        the canonical schema used by internal callers (e.g. end-of-day OHLCV
        batch completion, gap recovery, option-chain snapshot).

        Parameters
        ----------
        market:
            Market identifier, e.g. ``"NSE"``, ``"NFO"``, ``"CRYPTO"``.
        symbol:
            Trading symbol, e.g. ``"RELIANCE"``, ``"NIFTY"``, ``"BTC"``.
        interval:
            Canonical interval string, e.g. ``"1m"``, ``"5m"``, ``"1d"``.
        date:
            Session / reference date as ``"YYYY-MM-DD"`` (IST trading day for
            Indian markets, UTC date for crypto).
        record_count:
            Number of records (candles, ticks, …) in the completed dataset.

        Returns
        -------
        str
            Stream entry ID, or ``""`` when Event Bus is unavailable.
        """
        published_at = datetime.now(tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.%f"
        )[:-3] + "Z"

        event_data: dict[str, Any] = {
            "dataType": "HISTORICAL_OHLCV",
            "market": market,
            "symbol": symbol,
            "interval": interval,
            "date": date,
            "record_count": record_count,
            "published_at": published_at,
        }

        return await self.publish_dataset_ready(event_data)

    def get_stream_status(self) -> dict[str, Any]:
        """Return the current streaming status snapshot.

        Returns
        -------
        dict with keys:
            ``subscribedSymbols``  — list of currently tracked symbols
            ``ticksPublished``     — total ticks published since start
            ``duplicateCount``     — number of duplicate ticks detected
            ``validationFailures`` — failure counts by type
            ``lastPublishedAt``    — UTC ISO-8601 of last tick, or ``None``
            ``brokerConnections``  — connection state per broker
        """
        return {
            "subscribedSymbols": sorted(self._subscribed_symbols),
            "ticksPublished": self._ticks_published,
            "duplicateCount": self._duplicate_count,
            "validationFailures": dict(self._validation_failures),
            "lastPublishedAt": self._last_published_at,
            "brokerConnections": dict(self._broker_connections),
        }

    # ------------------------------------------------------------------
    # Broker connection state management
    # (called by respective stream adapter modules)
    # ------------------------------------------------------------------

    def set_broker_connection(self, broker: str, connected: bool) -> None:
        """Update the connection state for a broker.

        Parameters
        ----------
        broker:
            One of ``"angelOne"``, ``"upstox"``, ``"binance"``.
        connected:
            ``True`` when connected, ``False`` when disconnected.
        """
        if broker in self._broker_connections:
            self._broker_connections[broker] = connected
            logger.info(
                "broker_connection_state_changed",
                extra={"broker": broker, "connected": connected},
            )

    def record_validation_failure(self, failure_type: str) -> None:
        """Increment the validation failure counter for a given type.

        Parameters
        ----------
        failure_type:
            A string label for the failure category, e.g.
            ``"schema_validation"``, ``"ohlcv_invariant"``.
        """
        self._validation_failures[failure_type] = (
            self._validation_failures.get(failure_type, 0) + 1
        )

    def subscribe_symbol(self, symbol: str) -> None:
        """Register a symbol as having an active subscriber.

        Called by the WebSocket endpoint layer when a consumer subscribes.
        """
        self._subscribed_symbols.add(symbol)

    def unsubscribe_symbol(self, symbol: str) -> None:
        """Remove a symbol from the active subscriber set.

        Called by the WebSocket endpoint layer when a consumer unsubscribes
        or disconnects.
        """
        self._subscribed_symbols.discard(symbol)
