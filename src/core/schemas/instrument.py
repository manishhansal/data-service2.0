"""
Canonical Pydantic v2 schemas for instruments, F&O universe snapshots,
and instrument lifecycle events.

All provider-specific token fields are excluded from the default serialised
output. Call ``instrument.with_provider_tokens()`` or set
``include={"angelToken", ...}`` explicitly when provider tokens are needed
(e.g. when ``?include=providerTokens`` is requested by an authenticated
consumer).

Requirements: 2.2, 2.7, 11.4
"""

from __future__ import annotations

from datetime import date
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class InstrumentType(str, Enum):
    """NSE/BSE instrument type classification."""

    EQ = "EQ"          # Equity cash
    FUTIDX = "FUTIDX"  # Index future
    FUTSTK = "FUTSTK"  # Stock future
    OPTIDX = "OPTIDX"  # Index option
    OPTSTK = "OPTSTK"  # Stock option
    ETF = "ETF"        # Exchange-traded fund
    IDX = "IDX"        # Index (reference)


class ExchangeEnum(str, Enum):
    """Exchange identifiers supported by the platform."""

    NSE = "NSE"   # National Stock Exchange — cash segment
    NFO = "NFO"   # NSE F&O segment
    BSE = "BSE"   # Bombay Stock Exchange — cash segment
    BFO = "BFO"   # BSE F&O segment
    MCX = "MCX"   # Multi Commodity Exchange


class SegmentEnum(str, Enum):
    """Market segment classification."""

    EQ = "EQ"    # Equity cash
    FO = "FO"    # Futures & Options
    CD = "CD"    # Currency Derivatives
    COM = "COM"  # Commodity
    CDS = "CDS"  # Currency Derivatives Segment


class ChangeType(str, Enum):
    """Type of change in an instrument lifecycle event."""

    ADDED = "ADDED"
    REMOVED = "REMOVED"
    SUSPENDED = "SUSPENDED"


# ---------------------------------------------------------------------------
# Instrument model
# ---------------------------------------------------------------------------

# Provider token field names — excluded from the default serialised output
# (Requirement 2.7, 11.4).  Collected here so tests and other modules can
# reference them without string literals.
PROVIDER_TOKEN_FIELDS: frozenset[str] = frozenset(
    {"angelToken", "angelSymbol", "upstoxKey", "upstoxSymbol"}
)


class Instrument(BaseModel):
    """Canonical representation of a tradeable instrument.

    Provider token fields (``angelToken``, ``angelSymbol``, ``upstoxKey``,
    ``upstoxSymbol``) are **excluded** from the default ``.model_dump()``
    and ``.model_dump_json()`` output so they are never sent in consumer API
    responses unless explicitly requested.

    Use ``with_provider_tokens()`` to obtain a dict that includes the token
    fields, or call ``.model_dump(exclude=set())`` to override the
    per-field excludes programmatically.
    """

    model_config = ConfigDict(populate_by_name=True)

    # ── Core identity ────────────────────────────────────────────────────
    instrumentId: str = Field(description="Platform-canonical internal ID (UUID or deterministic hash).")
    tradingSymbol: str = Field(description="Exchange trading symbol, e.g. 'NIFTY25JANFUT'.")
    displaySymbol: Optional[str] = Field(default=None, description="Human-readable label, e.g. 'NIFTY Jan 2025 Fut'.")
    isin: Optional[str] = Field(default=None, description="ISIN code (equities only).")

    # ── Exchange classification ──────────────────────────────────────────
    exchange: ExchangeEnum
    segment: SegmentEnum
    instrumentType: InstrumentType
    underlying: Optional[str] = Field(
        default=None,
        description="Underlying symbol for derivatives, e.g. 'NIFTY'.",
    )

    # ── Derivative-specific ──────────────────────────────────────────────
    expiry: Optional[date] = Field(
        default=None,
        description="Expiry date as ISO-8601 UTC date. Null for non-derivative instruments.",
    )
    strike: Optional[float] = Field(
        default=None,
        description="Option strike price. Null for non-option instruments.",
    )
    optionType: Optional[Literal["CE", "PE"]] = Field(
        default=None,
        description="Option type: 'CE' (call) or 'PE' (put). Null for non-option instruments.",
    )

    # ── Contract specs ───────────────────────────────────────────────────
    lotSize: int = Field(default=1, ge=1, description="Contract lot size in units/shares.")
    tickSize: float = Field(default=0.05, gt=0.0, description="Minimum price movement.")

    # ── Lifecycle tracking ────────────────────────────────────────────────
    activeFrom: date = Field(description="Date from which this instrument record is valid.")
    activeTo: Optional[date] = Field(
        default=None,
        description="Date on which this record became inactive. Null means currently active.",
    )

    # ── Provider token mappings (excluded from consumer responses) ───────
    # These fields carry exchange-specific identifiers used internally by
    # the Provider_Gateway to resolve canonical instrumentId → provider token.
    # They MUST NOT appear in any consumer-facing API response unless the
    # consumer explicitly passes ?include=providerTokens (Requirement 2.7).
    angelToken: Optional[str] = Field(
        default=None,
        exclude=True,
        description="Angel One SmartAPI token for this instrument.",
    )
    angelSymbol: Optional[str] = Field(
        default=None,
        exclude=True,
        description="Angel One trading symbol.",
    )
    upstoxKey: Optional[str] = Field(
        default=None,
        exclude=True,
        description="Upstox instrument key (e.g. 'NSE_FO|12345').",
    )
    upstoxSymbol: Optional[str] = Field(
        default=None,
        exclude=True,
        description="Upstox trading symbol.",
    )

    # ── Methods ──────────────────────────────────────────────────────────

    def with_provider_tokens(self) -> dict:
        """Return a dict representation that includes the provider token fields.

        Used when the consumer has explicitly requested ``?include=providerTokens``
        on an authenticated request (Requirement 2.7, 11.4).

        Returns a plain ``dict`` so that the caller decides how to serialise it
        (e.g. via FastAPI ``jsonable_encoder``).
        """
        # model_dump respects the per-field ``exclude=True`` annotation by default.
        # Passing ``exclude={}`` does NOT override per-field excludes in Pydantic v2;
        # we therefore build the output by merging the public dict with the token
        # fields explicitly.
        base = self.model_dump()
        for field_name in PROVIDER_TOKEN_FIELDS:
            base[field_name] = getattr(self, field_name)
        return base


# ---------------------------------------------------------------------------
# F&O Universe Snapshot
# ---------------------------------------------------------------------------


class FnoUniverseSnapshot(BaseModel):
    """Immutable snapshot of the NSE F&O eligible universe at a point in time.

    A new snapshot is generated at 08:45 IST on every trading day if the
    checksum (SHA-256 of sorted instrument IDs) differs from the previous
    snapshot.  Identical checksums → no new snapshot is written.

    Requirements: 11.1, 11.2, 11.3
    """

    model_config = ConfigDict(populate_by_name=True)

    snapshotVersion: int = Field(
        ge=1,
        description="Monotonically increasing version counter.",
    )
    checksum: str = Field(
        description="SHA-256 hex digest of the sorted instrument ID list.",
    )
    generatedAt: str = Field(
        description="UTC ISO-8601 timestamp when this snapshot was produced.",
    )
    effectiveFrom: date = Field(
        description="IST trading date from which this snapshot is effective.",
    )
    effectiveTo: Optional[date] = Field(
        default=None,
        description="Date on which this snapshot was superseded. Null when ACTIVE.",
    )
    fnoEquityCount: int = Field(
        ge=0,
        description="Number of F&O-eligible equity instruments in this snapshot.",
    )
    fnoIndexCount: int = Field(
        ge=0,
        description="Number of F&O-eligible index instruments in this snapshot.",
    )
    constituentCount: int = Field(
        ge=0,
        description="Total number of constituent instruments (equities + indices).",
    )
    status: Literal["ACTIVE", "SUPERSEDED"] = Field(
        default="ACTIVE",
        description="ACTIVE = currently in use; SUPERSEDED = replaced by a newer snapshot.",
    )


# ---------------------------------------------------------------------------
# Instrument Lifecycle Event
# ---------------------------------------------------------------------------


class InstrumentLifecycleEvent(BaseModel):
    """Records a single change to the F&O universe between two snapshots.

    Emitted whenever the difference between two consecutive snapshots reveals
    an instrument that was ADDED, REMOVED, or SUSPENDED.

    Requirements: 2.4, 11.2
    """

    model_config = ConfigDict(populate_by_name=True)

    symbol: str = Field(description="Trading symbol of the affected instrument.")
    changeType: ChangeType = Field(
        description="Nature of the change: ADDED, REMOVED, or SUSPENDED.",
    )
    effectiveDate: date = Field(
        description="IST trading date on which the change takes effect.",
    )
    source: str = Field(
        description="Upstream source that published this change (e.g. 'NSE_CIRCULAR').",
    )
    snapshotVersion: int = Field(
        ge=1,
        description="Version of the FnoUniverseSnapshot that introduced this change.",
    )


# ---------------------------------------------------------------------------
# Market session enumerations
# ---------------------------------------------------------------------------


class SessionPhase(str, Enum):
    """NSE market session phase classification.

    Each IST clock instant maps to exactly one phase.  Phase boundaries are
    inclusive at the start time and exclusive at the end time.

    See design §NSE Session Phase State Machine for the full state diagram.
    Requirements: 12.1, 12.2
    """

    PRE_OPEN = "PRE_OPEN"                           # 09:00–09:08 IST
    PRE_OPEN_CALL_AUCTION = "PRE_OPEN_CALL_AUCTION" # 09:08–09:15 IST
    REGULAR = "REGULAR"                             # 09:15–15:30 IST (13:00 on half-days)
    POST_MARKET = "POST_MARKET"                     # 15:30–16:00 IST (13:00–13:30 on half-days)
    CLOSED = "CLOSED"                               # All other instants, holidays, weekends
    MUHURAT = "MUHURAT"                             # Diwali Muhurat session (special case)
