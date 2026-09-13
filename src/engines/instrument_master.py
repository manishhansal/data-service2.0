"""
src/engines/instrument_master.py

InstrumentMaster service — canonical registry for all tradeable instruments
with point-in-time tracking.

Responsibilities:
- Load the full instrument universe from the ``instrument_master`` PostgreSQL
  table into an in-memory dict at startup.
- Serve fast O(1) single-instrument lookups by ``instrumentId``.
- Serve filtered, point-in-time-correct searches across the universe.
- Resolve provider-specific tokens (Angel One, Upstox) for the Provider_Gateway
  without exposing those tokens in consumer-facing API responses.

Point-in-time contract:
  An instrument is considered *active on date D* when:
    ``activeFrom <= D``  AND  (``activeTo is None`` OR ``activeTo >= D``)

  All search queries default to ``active_only=True`` which applies this filter
  against ``date.today()``.  Pass ``active_only=False`` to include expired and
  future instruments.

Requirements: 2.3, 2.7, 2.8
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from src.core.schemas.instrument import (
    ExchangeEnum,
    Instrument,
    InstrumentType,
    SegmentEnum,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Column → field mapping
#
# The database uses snake_case column names; the canonical Pydantic schema uses
# camelCase.  This mapping is used in ``_row_to_instrument`` to translate DB
# rows into Instrument objects without hard-coding the mapping in multiple
# places.
# ---------------------------------------------------------------------------

_DB_COLUMNS = [
    "instrument_id",
    "trading_symbol",
    "display_symbol",
    "isin",
    "exchange",
    "segment",
    "instrument_type",
    "underlying",
    "expiry",
    "strike",
    "option_type",
    "lot_size",
    "tick_size",
    "active_from",
    "active_to",
    "angel_token",
    "angel_symbol",
    "upstox_key",
    "upstox_symbol",
]

_SELECT_SQL = f"SELECT {', '.join(_DB_COLUMNS)} FROM instrument_master"


def _row_to_instrument(row: dict) -> Instrument:
    """Map a raw DB row dict to an ``Instrument`` Pydantic model.

    Pydantic v2 coerces string enum values at validation time, so we can pass
    the raw strings from the database directly.
    """
    return Instrument(
        instrumentId=row["instrument_id"],
        tradingSymbol=row["trading_symbol"],
        displaySymbol=row.get("display_symbol"),
        isin=row.get("isin"),
        exchange=row["exchange"],
        segment=row["segment"],
        instrumentType=row["instrument_type"],
        underlying=row.get("underlying"),
        expiry=row.get("expiry"),
        strike=row.get("strike"),
        optionType=row.get("option_type"),
        lotSize=row.get("lot_size", 1),
        tickSize=row.get("tick_size", 0.05),
        activeFrom=row["active_from"],
        activeTo=row.get("active_to"),
        # Provider tokens — stored on the model but excluded from consumer serialisation
        angelToken=row.get("angel_token"),
        angelSymbol=row.get("angel_symbol"),
        upstoxKey=row.get("upstox_key"),
        upstoxSymbol=row.get("upstox_symbol"),
    )


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class InstrumentMasterService:
    """In-memory instrument registry populated from the ``instrument_master``
    PostgreSQL table at startup.

    The service is intentionally *not* a singleton; the application lifespan
    handler constructs one instance, loads the data, and attaches it to
    ``app.state``.

    Thread/concurrency safety: the ``_instruments`` dict is populated once
    during startup (``load_from_db``) and is treated as read-only afterwards.
    Concurrent readers therefore need no locking.

    Attributes:
        _instruments: Dict keyed by ``instrumentId``, values are ``Instrument``
            objects including provider token fields.
    """

    def __init__(self) -> None:
        self._instruments: dict[str, Instrument] = {}

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    async def load_from_db(self, engine: AsyncEngine) -> None:
        """Load all instruments from the database into the in-memory store.

        Replaces any existing in-memory data.  Safe to call multiple times
        (e.g. for a hot-reload on snapshot refresh).

        Args:
            engine: A live ``AsyncEngine``.  The method acquires and releases
                its own connection; no external session is required.

        Raises:
            Exception: Any database error propagates so the caller can decide
                whether to enter degraded mode.  The in-memory store is *not*
                cleared if the load fails mid-way; the previous snapshot is
                retained.
        """
        rows_loaded = 0
        new_store: dict[str, Instrument] = {}

        async with engine.connect() as conn:
            result = await conn.execute(text(_SELECT_SQL))
            # ``mappings()`` returns each row as a ``RowMapping`` (dict-like).
            for mapping in result.mappings():
                row = dict(mapping)
                try:
                    instrument = _row_to_instrument(row)
                    new_store[instrument.instrumentId] = instrument
                    rows_loaded += 1
                except Exception as exc:  # noqa: BLE001
                    # Skip invalid rows; log and continue to maximise coverage.
                    logger.warning(
                        "instrument_master_row_skipped",
                        extra={
                            "instrument_id": row.get("instrument_id"),
                            "error": str(exc),
                        },
                    )

        # Atomic swap — readers never see a half-loaded store.
        self._instruments = new_store

        logger.info(
            "instrument_master_loaded",
            extra={"count": rows_loaded},
        )

    # ------------------------------------------------------------------
    # Single lookup
    # ------------------------------------------------------------------

    def get_instrument(self, instrument_id: str) -> Optional[Instrument]:
        """Return the canonical ``Instrument`` for *instrument_id*, or ``None``.

        This method returns the instrument regardless of its active/expired
        status; use ``search_instruments(active_only=True)`` when you need to
        restrict to currently active instruments.

        Args:
            instrument_id: Platform canonical instrument ID (e.g.
                ``"NSE:RELIANCE:EQ"``).

        Returns:
            ``Instrument`` if found; ``None`` if no record exists for the given
            ID.
        """
        return self._instruments.get(instrument_id)

    # ------------------------------------------------------------------
    # Search / filter
    # ------------------------------------------------------------------

    def search_instruments(
        self,
        exchange: Optional[ExchangeEnum | str] = None,
        instrument_type: Optional[InstrumentType | str] = None,
        underlying: Optional[str] = None,
        segment: Optional[SegmentEnum | str] = None,
        expiry: Optional[date] = None,
        active_only: bool = True,
        as_of: Optional[date] = None,
    ) -> list[Instrument]:
        """Search the in-memory instrument store with optional filters.

        All filter parameters are individually optional; passing no filters
        with ``active_only=True`` returns the full active universe.

        Point-in-time accuracy:
            When ``active_only=True`` (the default), only instruments whose
            ``activeFrom <= as_of`` AND (``activeTo is None`` OR
            ``activeTo >= as_of``) are returned.

            ``as_of`` defaults to ``date.today()`` when not provided.

        Args:
            exchange: Restrict to this exchange (e.g. ``ExchangeEnum.NSE``).
            instrument_type: Restrict to this instrument type (e.g.
                ``InstrumentType.OPTIDX``).
            underlying: Restrict to derivatives on this underlying (e.g.
                ``"NIFTY"``).
            segment: Restrict to this market segment (e.g. ``SegmentEnum.FO``).
            expiry: Restrict to instruments expiring on exactly this date.
            active_only: When ``True`` (default), exclude instruments whose
                ``activeTo`` is before *as_of* (expired contracts) and exclude
                instruments whose ``activeFrom`` is after *as_of* (future
                listings).
            as_of: Reference date for the point-in-time filter.  Defaults to
                ``date.today()``.

        Returns:
            List of matching ``Instrument`` objects.  May be empty.
        """
        reference_date = as_of if as_of is not None else date.today()

        # Normalise enum filters so comparison works whether callers pass a
        # string or an enum member.
        _exchange = ExchangeEnum(exchange) if isinstance(exchange, str) else exchange
        _instrument_type = (
            InstrumentType(instrument_type)
            if isinstance(instrument_type, str)
            else instrument_type
        )
        _segment = SegmentEnum(segment) if isinstance(segment, str) else segment

        results: list[Instrument] = []

        for instrument in self._instruments.values():
            # ── Point-in-time active filter ───────────────────────────────
            if active_only:
                if instrument.activeFrom > reference_date:
                    continue
                if instrument.activeTo is not None and instrument.activeTo < reference_date:
                    continue

            # ── Field filters ─────────────────────────────────────────────
            if _exchange is not None and instrument.exchange != _exchange:
                continue
            if _instrument_type is not None and instrument.instrumentType != _instrument_type:
                continue
            if underlying is not None and instrument.underlying != underlying:
                continue
            if _segment is not None and instrument.segment != _segment:
                continue
            if expiry is not None and instrument.expiry != expiry:
                continue

            results.append(instrument)

        return results

    # ------------------------------------------------------------------
    # Provider token resolution
    # ------------------------------------------------------------------

    def resolve_provider_tokens(
        self, instrument_id: str, provider_id: str
    ) -> dict[str, Optional[str]]:
        """Return provider-specific token fields for *instrument_id*.

        This method is intended exclusively for internal use by the
        Provider_Gateway to translate canonical instrument IDs into provider
        tokens.  The returned dict MUST NOT be forwarded to consumer API
        responses.

        Supported *provider_id* values:
            - ``"angel_one"`` → ``{"angelToken": ..., "angelSymbol": ...}``
            - ``"upstox"``    → ``{"upstoxKey": ..., "upstoxSymbol": ...}``

        Args:
            instrument_id: Platform canonical instrument ID.
            provider_id: Lower-case provider identifier string.

        Returns:
            A dict of provider-specific fields.  Values may be ``None`` if the
            instrument has no mapping for the requested provider.  Returns an
            empty dict ``{}`` if *instrument_id* is not found or *provider_id*
            is not recognised.
        """
        instrument = self._instruments.get(instrument_id)
        if instrument is None:
            logger.debug(
                "resolve_provider_tokens_miss",
                extra={"instrument_id": instrument_id, "provider": provider_id},
            )
            return {}

        provider_lower = provider_id.lower()

        if provider_lower == "angel_one":
            return {
                "angelToken": instrument.angelToken,
                "angelSymbol": instrument.angelSymbol,
            }

        if provider_lower == "upstox":
            return {
                "upstoxKey": instrument.upstoxKey,
                "upstoxSymbol": instrument.upstoxSymbol,
            }

        logger.warning(
            "resolve_provider_tokens_unknown_provider",
            extra={"provider": provider_id},
        )
        return {}

    # ------------------------------------------------------------------
    # Introspection helpers
    # ------------------------------------------------------------------

    def __len__(self) -> int:
        """Return the number of instruments currently in the in-memory store."""
        return len(self._instruments)

    def is_loaded(self) -> bool:
        """Return ``True`` when the store has been populated (non-empty)."""
        return bool(self._instruments)
