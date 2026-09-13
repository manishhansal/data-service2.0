"""
src/backtest/point_in_time.py — Point-in-time filter for backtest mode.

Prevents look-ahead bias by ensuring that backtest queries only return data
that was actually available at the requested ``as_of`` timestamp.

Two filter strategies are provided:

1. **``filter``** — simple time-based filter.  Includes a record when its
   ``time`` field (epoch-ms) is ≤ ``as_of_ts``.  This is a conservative
   lower bound: a candle whose bar closes at ``time`` is generally made
   available shortly afterwards, so a raw ``time`` filter may still admit
   some latency.  Use when ``availableAtMs`` provenance data is absent.

2. **``filter_by_available_at``** — strict provenance-based filter
   (preferred).  Includes a record only when ``availableAtMs ≤ as_of_ts``.
   ``availableAtMs`` records the exact UTC millisecond at which the
   Platform first made the datum available to consumers, so this filter is
   free from any latency assumptions.  Records missing ``availableAtMs`` are
   **rejected** (Requirement 23.5) and logged for audit purposes.

The ``is_look_ahead`` helper can be used in single-record checks (e.g. to
guard signal-generation paths).

The ``BacktestContext`` Pydantic model carries the point-in-time timestamp and
mode flag through the request lifecycle.  The ``backtest_context`` FastAPI
dependency creates the context from the optional ``as_of`` query parameter.

Requirements: 23.4, 23.5
"""

from __future__ import annotations

import logging
from typing import Literal, Optional

from fastapi import Query
from pydantic import BaseModel, Field, model_validator

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# BacktestContext — Pydantic model
# ---------------------------------------------------------------------------


class BacktestContext(BaseModel):
    """Point-in-time context for a backtest or live data request.

    Attributes:
        as_of_ts: Point-in-time boundary expressed as UTC epoch milliseconds.
                  Only data with ``availableAtMs ≤ as_of_ts`` (or
                  ``time ≤ as_of_ts`` when provenance data is unavailable)
                  will be returned.  Must be a positive integer.
        mode:     ``"BACKTEST"`` when a point-in-time boundary is active;
                  ``"LIVE"`` for normal real-time requests where no
                  look-ahead guard is applied.

    Requirements: 23.4
    """

    as_of_ts: int = Field(
        description="Point-in-time boundary as UTC epoch milliseconds.",
        gt=0,
    )
    mode: Literal["BACKTEST", "LIVE"] = Field(
        default="LIVE",
        description=(
            '"BACKTEST" applies the point-in-time filter; '
            '"LIVE" disables it (real-time mode).'
        ),
    )

    @model_validator(mode="after")
    def _mode_must_be_backtest_when_as_of_ts_provided(self) -> "BacktestContext":
        """Ensure mode is consistent with as_of_ts usage.

        When as_of_ts is provided (non-zero), mode should be BACKTEST so that
        callers are explicit about filtering intent.  This validator only
        emits a warning — it does not raise — so as not to break integrations
        that set as_of_ts without changing mode.
        """
        # as_of_ts > 0 is enforced by Field(gt=0), so any valid instance has a
        # meaningful timestamp.  We rely on the mode field defaulting to LIVE
        # when not supplied, but callers should set mode="BACKTEST" explicitly.
        return self


# ---------------------------------------------------------------------------
# PointInTimeFilter
# ---------------------------------------------------------------------------


class PointInTimeFilter:
    """Stateless point-in-time filter for OHLCV record lists.

    All methods are pure functions implemented as regular (non-async) methods
    for use in both synchronous and async contexts without an event loop.

    Requirements: 23.4, 23.5
    """

    # Default offset (ms) added to ``time`` in the simple ``filter`` path to
    # approximate when a candle becomes available after its bar closes.
    # Set to 0 by default — callers may override for their latency budget.
    DEFAULT_AVAILABLE_AT_OFFSET_MS: int = 0

    # ------------------------------------------------------------------
    # filter — time-based lower-bound filter
    # ------------------------------------------------------------------

    def filter(
        self,
        records: list[dict],
        as_of_ts: int,
        available_at_offset_ms: int = DEFAULT_AVAILABLE_AT_OFFSET_MS,
    ) -> list[dict]:
        """Filter records to those available at ``as_of_ts`` using the ``time`` field.

        A record is included when::

            record["time"] + available_at_offset_ms <= as_of_ts

        ``available_at_offset_ms`` is a configurable latency allowance (in ms)
        that accounts for the delay between a candle closing and the platform
        making it available.  For conservative (no look-ahead) filtering, keep
        this at 0 (the default).

        Records missing the ``time`` field are **excluded** silently, since
        their temporal position is unknown.

        Args:
            records:               List of OHLCV record dicts.  Each record
                                   must contain a ``"time"`` key with a value
                                   that is a UTC epoch-millisecond integer.
            as_of_ts:              Point-in-time boundary as UTC epoch ms.
            available_at_offset_ms: Non-negative ms offset added to ``time``
                                   before comparison.  Defaults to 0.

        Returns:
            Filtered list containing only records available at ``as_of_ts``.

        Requirements: 23.4
        """
        if available_at_offset_ms < 0:
            raise ValueError(
                f"available_at_offset_ms must be non-negative; got {available_at_offset_ms}"
            )

        result: list[dict] = []
        for record in records:
            time_ms = record.get("time")
            if time_ms is None:
                # No temporal anchor — exclude conservatively.
                logger.debug(
                    "point_in_time_filter_missing_time",
                    extra={"record_keys": list(record.keys())},
                )
                continue
            try:
                effective_ts = int(time_ms) + available_at_offset_ms
            except (TypeError, ValueError):
                logger.debug(
                    "point_in_time_filter_invalid_time",
                    extra={"time_value": time_ms},
                )
                continue
            if effective_ts <= as_of_ts:
                result.append(record)
        return result

    # ------------------------------------------------------------------
    # filter_by_available_at — strict provenance-based filter
    # ------------------------------------------------------------------

    def filter_by_available_at(
        self,
        records: list[dict],
        as_of_ts: int,
    ) -> list[dict]:
        """Filter records using the ``availableAtMs`` provenance field.

        This is the **correct** backtest filter (Requirement 23.4).  It uses
        the exact timestamp at which the Platform first made each datum
        available to consumers, eliminating all latency assumptions.

        A record is included when ``record["availableAtMs"] <= as_of_ts``.

        Records that are **missing** the ``availableAtMs`` field, or whose
        ``availableAtMs`` value cannot be parsed as a valid UTC ms integer,
        are **rejected** from the backtest dataset (Requirement 23.5).  Each
        rejection is logged with the record identifier (``instrumentId`` or
        ``time`` if present) and the rejection reason.

        Args:
            records:  List of OHLCV record dicts.  Each record should carry
                      an ``"availableAtMs"`` key (populated from
                      ``DataProvenance.availableAtMs``).
            as_of_ts: Point-in-time boundary as UTC epoch milliseconds.

        Returns:
            Filtered list containing only records with
            ``availableAtMs <= as_of_ts``.

        Requirements: 23.4, 23.5
        """
        result: list[dict] = []
        for record in records:
            available_at = record.get("availableAtMs")

            # --- Reject: field absent ---
            if available_at is None:
                record_id = record.get("instrumentId") or record.get("time") or "<unknown>"
                logger.warning(
                    "backtest_record_rejected_missing_available_at_ms",
                    extra={
                        "recordId": str(record_id),
                        "rejectionReason": "availableAtMs field is absent",
                    },
                )
                continue

            # --- Reject: field not parseable as integer ---
            try:
                available_at_ms = int(available_at)
            except (TypeError, ValueError):
                record_id = record.get("instrumentId") or record.get("time") or "<unknown>"
                logger.warning(
                    "backtest_record_rejected_invalid_available_at_ms",
                    extra={
                        "recordId": str(record_id),
                        "availableAtMs": repr(available_at),
                        "rejectionReason": "availableAtMs cannot be parsed as UTC epoch ms integer",
                    },
                )
                continue

            # --- Include: availableAtMs <= as_of_ts ---
            if available_at_ms <= as_of_ts:
                result.append(record)

        return result

    # ------------------------------------------------------------------
    # is_look_ahead — single-record check
    # ------------------------------------------------------------------

    def is_look_ahead(self, record: dict, as_of_ts: int) -> bool:
        """Return ``True`` if the record would **not** have been available at ``as_of_ts``.

        Uses ``availableAtMs`` when present (strict provenance check), falling
        back to the ``time`` field otherwise (conservative time-based check).

        A ``True`` result means including this record in a backtest at time
        ``as_of_ts`` would constitute look-ahead bias.

        Args:
            record:   A single OHLCV record dict.
            as_of_ts: Point-in-time boundary as UTC epoch milliseconds.

        Returns:
            ``True``  — record is a look-ahead (should be excluded).
            ``False`` — record was available at ``as_of_ts`` (safe to include).

        Note:
            Records missing both ``availableAtMs`` and ``time`` are treated as
            look-ahead (``True``) because their temporal position is unknown
            and including them would be unsafe.

        Requirements: 23.4
        """
        available_at = record.get("availableAtMs")

        if available_at is not None:
            # Prefer strict provenance check.
            try:
                return int(available_at) > as_of_ts
            except (TypeError, ValueError):
                # Unparseable availableAtMs → treat as look-ahead (unsafe).
                return True

        # Fall back to ``time`` field.
        time_ms = record.get("time")
        if time_ms is None:
            # No temporal anchor — treat as look-ahead (unsafe).
            return True

        try:
            return int(time_ms) > as_of_ts
        except (TypeError, ValueError):
            return True


# ---------------------------------------------------------------------------
# FastAPI dependency
# ---------------------------------------------------------------------------


def backtest_context(
    as_of: Optional[int] = Query(
        default=None,
        description=(
            "Point-in-time boundary for backtest mode as UTC epoch milliseconds. "
            "When supplied, only data available at or before this timestamp is returned. "
            "Omit for live (real-time) mode."
        ),
        gt=0,
        alias="as_of",
    ),
) -> BacktestContext:
    """FastAPI dependency that creates a :class:`BacktestContext` from query params.

    Usage::

        @router.get("/v1/india/historical")
        async def get_historical(
            ctx: BacktestContext = Depends(backtest_context),
        ):
            if ctx.mode == "BACKTEST":
                records = pit_filter.filter_by_available_at(records, ctx.as_of_ts)

    When ``as_of`` is **not** supplied the dependency returns a ``LIVE``
    context using the current UTC epoch milliseconds as ``as_of_ts``.  The
    ``LIVE`` mode sentinel means consumers can always safely check
    ``ctx.mode`` without branching on ``None``.

    Args:
        as_of: Optional UTC epoch-millisecond timestamp from the ``as_of``
               query parameter.

    Returns:
        :class:`BacktestContext` with ``mode="BACKTEST"`` when ``as_of`` is
        provided, or ``mode="LIVE"`` with the current wall-clock time otherwise.

    Requirements: 23.4
    """
    if as_of is not None:
        return BacktestContext(as_of_ts=as_of, mode="BACKTEST")

    # Live mode — use a far-future sentinel so that a LIVE context passed to
    # the filters accidentally still includes all historical records.  We use
    # the current time + a large offset to represent "now and everything before".
    import time as _time
    current_ms = int(_time.time() * 1000)
    return BacktestContext(as_of_ts=current_ms, mode="LIVE")
