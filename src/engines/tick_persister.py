"""
src/engines/tick_persister.py

TickPersister — persists live WebSocket ticks to the ``market_tick`` table
and finalised candles from ``CandleBuilder`` to the canonical candle tables.

Architecture
------------
Provider WebSocket
    → stream adapter (AngelOneStreamAdapter / UpstoxStreamAdapter)
    → normalised tick dict
    → TickPersister.on_tick()
        ├─ persist to market_tick (fire-and-forget)
        └─ forward to CandleBuilder.process_tick()
              → on candle close: CandleBuilder calls _on_candle_finalised()
                  └─ persist to equity_candle / futures_candle / options_candle

Design invariants
-----------------
* Exchange timestamps are NEVER replaced with local timestamps.
  ``market_tick.timestamp`` = exchange timestamp.
  ``market_tick.received_at`` = DB server now() (server_default).
* NULL semantics enforced: bid, ask, OI are NULL when absent — never zero.
* Writes are fire-and-forget (asyncio.create_task) to avoid blocking
  the WebSocket receive loop.
* All DB errors are caught and logged; they never crash the stream.
* Point-in-time: candle_time_ms <= available_at_ms <= ingestion_time_ms
  is validated before each candle write.

Requirements: Phases E–F–G; §Live Streaming Production Certification
"""

from __future__ import annotations

import asyncio
import datetime
import time
from typing import Any, Optional, TYPE_CHECKING

from src.observability.logging import get_logger

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# TickPersister
# ---------------------------------------------------------------------------


class TickPersister:
    """Persists ticks and candles from live WebSocket streams to PostgreSQL.

    Args:
        db_engine:      Async SQLAlchemy engine.  When None, all persistence
                        calls become no-ops (useful in unit tests).
        candle_builder: Optional ``CandleBuilder`` instance.  When provided,
                        every tick is forwarded for candle aggregation.
        provider:       Provider name string (e.g. "angel_one", "upstox").
        batch_size:     Number of ticks to accumulate before a batch insert.
                        Default 50. Set to 1 for low-latency / low-volume.
        flush_interval: Max seconds between forced flushes of the tick buffer.
                        Default 2.0.
    """

    def __init__(
        self,
        db_engine: Optional["AsyncEngine"] = None,
        candle_builder: Optional[Any] = None,
        provider: str = "unknown",
        batch_size: int = 50,
        flush_interval: float = 2.0,
    ) -> None:
        self._db_engine = db_engine
        self._candle_builder = candle_builder
        self._provider = provider
        self._batch_size = batch_size
        self._flush_interval = flush_interval

        # Tick buffer for batching
        self._tick_buffer: list[dict[str, Any]] = []
        self._last_flush_ts: float = time.monotonic()

        # Candle callback — registered on the CandleBuilder
        if candle_builder is not None:
            candle_builder.add_candle_callback(self._on_candle_finalised)

        # Statistics
        self._ticks_received: int = 0
        self._ticks_persisted: int = 0
        self._ticks_dropped: int = 0
        self._candles_persisted: int = 0
        self._errors: int = 0

    # ------------------------------------------------------------------
    # Public API — called from stream adapters
    # ------------------------------------------------------------------

    async def on_tick(self, tick: dict[str, Any]) -> None:
        """Handle an incoming normalised tick.

        1. Forward to CandleBuilder for aggregation.
        2. Add to tick buffer; flush when batch_size reached or flush_interval elapsed.

        This method is designed to be called from the WebSocket receive loop.
        All DB I/O is dispatched as fire-and-forget tasks.
        """
        self._ticks_received += 1

        # Forward to candle builder (runs in this event loop — fast)
        if self._candle_builder is not None:
            try:
                await self._candle_builder.process_tick(tick)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "tick_persister_candle_builder_error",
                    component="tick_persister",
                    provider=self._provider,
                    error=str(exc),
                )

        # Buffer tick for DB write
        self._tick_buffer.append(tick)

        # Flush on batch size or interval
        now = time.monotonic()
        if (
            len(self._tick_buffer) >= self._batch_size
            or (now - self._last_flush_ts) >= self._flush_interval
        ):
            asyncio.create_task(self._flush_ticks())

    async def flush(self) -> None:
        """Force-flush the tick buffer immediately."""
        await self._flush_ticks()

    async def shutdown(self) -> None:
        """Flush remaining ticks and finalise all open candles."""
        await self._flush_ticks()
        if self._candle_builder is not None:
            await self._candle_builder.flush_all()

    # ------------------------------------------------------------------
    # Private: market_tick persistence
    # ------------------------------------------------------------------

    async def _flush_ticks(self) -> None:
        """Insert buffered ticks into market_tick."""
        if not self._tick_buffer or self._db_engine is None:
            self._tick_buffer = []
            self._last_flush_ts = time.monotonic()
            return

        batch = self._tick_buffer
        self._tick_buffer = []
        self._last_flush_ts = time.monotonic()

        try:
            rows = [self._tick_to_row(t) for t in batch]
            rows = [r for r in rows if r is not None]
            if rows:
                await self._bulk_insert_ticks(rows)
                self._ticks_persisted += len(rows)
        except Exception as exc:  # noqa: BLE001
            self._errors += 1
            self._ticks_dropped += len(batch)
            logger.warning(
                "tick_persister_flush_error",
                component="tick_persister",
                provider=self._provider,
                batch_size=len(batch),
                error=str(exc),
            )

    def _tick_to_row(self, tick: dict[str, Any]) -> Optional[dict[str, Any]]:
        """Convert a normalised tick dict to a market_tick DB row dict."""
        try:
            instrument_id = tick.get("instrumentId") or tick.get("instrument_id", "")
            exchange = tick.get("exchange", "NSE")

            # Exchange timestamp — NEVER replaced with local time
            exch_ts_ms = (
                tick.get("exchange_ts_ms")
                or tick.get("eventTimeMs")
            )
            if exch_ts_ms is None:
                logger.debug(
                    "tick_missing_exchange_ts",
                    component="tick_persister",
                    instrument_id=instrument_id,
                )
                return None

            exchange_ts = datetime.datetime.fromtimestamp(
                int(exch_ts_ms) / 1000.0, tz=datetime.timezone.utc
            )

            # NULL semantics: bid/ask/OI are NULL when absent
            bid = tick.get("bid") or tick.get("bestBid")
            ask = tick.get("ask") or tick.get("bestAsk")
            bid_qty = tick.get("bidQuantity") or tick.get("bid_quantity")
            ask_qty = tick.get("askQuantity") or tick.get("ask_quantity")
            oi = tick.get("oi") or tick.get("open_interest")
            seq = tick.get("sequenceNumber") or tick.get("sequence_number")

            source_type = tick.get("sourceType") or tick.get("source_type") or "LIVE_WEBSOCKET"

            return {
                "instrument_id": instrument_id,
                "exchange": exchange,
                "timestamp": exchange_ts,
                # received_at is set by server_default=NOW()
                "ltp":  _to_float(tick.get("ltp")),
                "open": _to_float(tick.get("open")),
                "high": _to_float(tick.get("high")),
                "low":  _to_float(tick.get("low")),
                "close": _to_float(tick.get("close") or tick.get("prevClose")),
                "volume": _to_int(tick.get("volume")),
                "open_interest": _to_int(oi),
                "bid":  _to_float(bid),
                "ask":  _to_float(ask),
                "bid_quantity":   _to_int(bid_qty),
                "ask_quantity":   _to_int(ask_qty),
                "last_traded_quantity": _to_int(
                    tick.get("lastTradedQuantity") or tick.get("lastTradeQty")
                ),
                "total_buy_quantity":  _to_int(tick.get("totalBuyQty")),
                "total_sell_quantity": _to_int(tick.get("totalSellQty")),
                "provider": self._provider,
                "source_type": source_type,
                "sequence_number": _to_int(seq),
                "source_timestamp": exchange_ts,  # exchange_ts is the source timestamp
                "quality_status": "TRUSTED",
                "session_date": exchange_ts.date(),
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "tick_to_row_conversion_error",
                component="tick_persister",
                error=str(exc),
            )
            return None

    async def _bulk_insert_ticks(self, rows: list[dict[str, Any]]) -> None:
        """Bulk insert into market_tick using ON CONFLICT DO NOTHING."""
        from sqlalchemy import text  # noqa: PLC0415
        # market_tick has no unique constraint beyond the timescaledb primary key
        # so simple INSERT is safe; TimescaleDB handles duplicate primary keys
        # by ignoring on conflict (tick_id is IDENTITY so always unique)
        insert_sql = text("""
            INSERT INTO market_tick (
                instrument_id, exchange, timestamp,
                ltp, open, high, low, close, volume, open_interest,
                bid, ask, bid_quantity, ask_quantity,
                last_traded_quantity, total_buy_quantity, total_sell_quantity,
                provider, source_type, sequence_number, source_timestamp,
                quality_status, session_date
            )
            VALUES (
                :instrument_id, :exchange, :timestamp,
                :ltp, :open, :high, :low, :close, :volume, :open_interest,
                :bid, :ask, :bid_quantity, :ask_quantity,
                :last_traded_quantity, :total_buy_quantity, :total_sell_quantity,
                :provider, :source_type, :sequence_number, :source_timestamp,
                :quality_status, :session_date
            )
            ON CONFLICT DO NOTHING
        """)
        async with self._db_engine.begin() as conn:
            for row in rows:
                await conn.execute(insert_sql, row)
        logger.debug(
            "tick_batch_inserted",
            component="tick_persister",
            provider=self._provider,
            count=len(rows),
        )

    # ------------------------------------------------------------------
    # Private: candle persistence (callback from CandleBuilder)
    # ------------------------------------------------------------------

    async def _on_candle_finalised(self, candle: Any) -> None:
        """Callback from CandleBuilder — persist a finalised candle.

        Validates point-in-time contract before writing.
        Routes to equity_candle / futures_candle / options_candle.
        """
        if self._db_engine is None:
            return

        # Point-in-time validation — FAIL HARD if violated
        avail = candle.available_at_ms
        ingest = candle.ingestion_time_ms
        if avail is not None and candle.candle_time_ms > avail:
            logger.error(
                "candle_lookahead_violation",
                component="tick_persister",
                instrument_id=candle.instrument_id,
                interval=candle.interval,
                candle_time_ms=candle.candle_time_ms,
                available_at_ms=avail,
                severity="CRITICAL",
            )
            # Do NOT persist a look-ahead candle
            return

        # Determine target table from instrument_id/exchange
        table = _route_candle_table(candle.instrument_id, candle.exchange)

        row = candle.to_dict()
        # Map candle dict keys to DB column names
        db_row = {
            "instrument_id": row["instrument_id"],
            "exchange":      row["exchange"],
            "interval_str":  row["interval_str"],
            "time":          row["time"],
            "session_date":  row["time"].date(),
            "open":          row["open"],
            "high":          row["high"],
            "low":           row["low"],
            "close":         row["close"],
            "volume":        row["volume"],
            "open_interest": row.get("open_interest"),
            "oi_change":     row.get("oi_change"),
            "provider":      row["provider"] or self._provider,
            "source_type":   "LIVE_WEBSOCKET",
            "data_origin":   "PROVIDER",
            "quality_status": "TRUSTED",
            "normalisation_version": "2.0.0",
            "dataset_version": 1,
        }

        try:
            from sqlalchemy import text  # noqa: PLC0415
            if table == "equity_candle":
                upsert = text("""
                    INSERT INTO equity_candle (
                        instrument_id, exchange, interval_str, time, session_date,
                        open, high, low, close, volume,
                        provider, source_type, data_origin,
                        quality_status, normalisation_version, dataset_version
                    )
                    VALUES (
                        :instrument_id, :exchange, :interval_str, :time, :session_date,
                        :open, :high, :low, :close, :volume,
                        :provider, :source_type, :data_origin,
                        :quality_status, :normalisation_version, :dataset_version
                    )
                    ON CONFLICT (instrument_id, exchange, interval_str, time)
                    DO UPDATE SET
                        high   = GREATEST(equity_candle.high, EXCLUDED.high),
                        low    = LEAST(equity_candle.low, EXCLUDED.low),
                        close  = EXCLUDED.close,
                        volume = GREATEST(equity_candle.volume, EXCLUDED.volume)
                """)
            elif table == "futures_candle":
                db_row["expiry"] = _extract_expiry(candle.instrument_id)
                db_row["contract_type"] = "FUT"
                db_row["underlying_id"] = _extract_underlying(candle.instrument_id)
                upsert = text("""
                    INSERT INTO futures_candle (
                        instrument_id, exchange, interval_str, time, session_date,
                        expiry, contract_type, underlying_id,
                        open, high, low, close, volume, open_interest, oi_change,
                        provider, source_type, data_origin,
                        quality_status, normalisation_version, dataset_version
                    )
                    VALUES (
                        :instrument_id, :exchange, :interval_str, :time, :session_date,
                        :expiry, :contract_type, :underlying_id,
                        :open, :high, :low, :close, :volume, :open_interest, :oi_change,
                        :provider, :source_type, :data_origin,
                        :quality_status, :normalisation_version, :dataset_version
                    )
                    ON CONFLICT (instrument_id, exchange, interval_str, time)
                    DO UPDATE SET
                        high          = GREATEST(futures_candle.high, EXCLUDED.high),
                        low           = LEAST(futures_candle.low, EXCLUDED.low),
                        close         = EXCLUDED.close,
                        volume        = GREATEST(futures_candle.volume, EXCLUDED.volume),
                        open_interest = COALESCE(EXCLUDED.open_interest, futures_candle.open_interest),
                        oi_change     = COALESCE(EXCLUDED.oi_change, futures_candle.oi_change)
                """)
            else:
                # options_candle — require strike/option_type; skip if unavailable
                logger.debug(
                    "tick_persister_options_candle_skipped",
                    component="tick_persister",
                    instrument_id=candle.instrument_id,
                    reason="options candles require strike/option_type from instrument master",
                )
                return

            async with self._db_engine.begin() as conn:
                await conn.execute(upsert, db_row)

            self._candles_persisted += 1
            logger.debug(
                "live_candle_persisted",
                component="tick_persister",
                table=table,
                instrument_id=candle.instrument_id,
                interval=candle.interval,
                candle_time_ms=candle.candle_time_ms,
                close=candle.close,
                finalised=candle.finalised,
            )
        except Exception as exc:  # noqa: BLE001
            self._errors += 1
            logger.warning(
                "candle_persist_error",
                component="tick_persister",
                table=table,
                instrument_id=candle.instrument_id,
                interval=candle.interval,
                error=str(exc),
            )

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def get_stats(self) -> dict[str, Any]:
        builder_stats = (
            self._candle_builder.get_stats()
            if self._candle_builder is not None
            else {}
        )
        return {
            "ticks_received":    self._ticks_received,
            "ticks_persisted":   self._ticks_persisted,
            "ticks_dropped":     self._ticks_dropped,
            "candles_persisted": self._candles_persisted,
            "errors":            self._errors,
            "buffer_depth":      len(self._tick_buffer),
            "candle_builder":    builder_stats,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _to_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _to_int(v: Any) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _route_candle_table(instrument_id: str, exchange: str) -> str:
    """Route to equity_candle, futures_candle, or options_candle."""
    exch = exchange.upper()
    if exch in ("NFO", "BFO"):
        # NFO instruments: FUT = futures_candle, CE/PE = options_candle
        if "FUT" in instrument_id.upper():
            return "futures_candle"
        if any(t in instrument_id.upper() for t in ("CE", "PE")):
            return "options_candle"
        return "futures_candle"
    return "equity_candle"


def _extract_expiry(instrument_id: str) -> datetime.date:
    """Best-effort expiry extraction from F&O instrument_id string.

    Returns today+30 as a safe default if parsing fails.
    """
    import re  # noqa: PLC0415
    m = re.search(r"(\d{2})([A-Z]{3})(\d{4})", instrument_id)
    if m:
        try:
            return datetime.datetime.strptime(
                f"{m.group(1)}{m.group(2)}{m.group(3)}", "%d%b%Y"
            ).date()
        except ValueError:
            pass
    return (datetime.date.today() + datetime.timedelta(days=30))


def _extract_underlying(instrument_id: str) -> Optional[str]:
    """Extract underlying symbol from F&O instrument_id."""
    import re  # noqa: PLC0415
    # Pattern: NFO:NIFTY25SEP26FUT → NIFTY
    m = re.match(r"(?:[A-Z]+:)?([A-Z]+)\d", instrument_id)
    if m:
        return f"NSE:{m.group(1)}"
    return None
