"""
src/db/models/option_chain.py

SQLAlchemy ORM models for option chain and Greeks storage:
  - OptionChainSnapshot  — point-in-time chain header
  - OptionChainContract  — per-strike rows (child of snapshot)
  - OptionGreeksSnapshot — IV + Greeks time-series (TimescaleDB hypertable)

Greeks are NEVER fabricated. All Greek fields are NULL when not supplied
by the provider or deterministically calculable from authoritative inputs.
Zero is NOT a substitute for a missing Greek value.
"""
from __future__ import annotations

import datetime
import uuid
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    Identity,
    Index,
    Numeric,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql.sqltypes import TIMESTAMP

from src.db.models.base import Base

_QUALITY_STATUS_CHECK = (
    "quality_status IN ("
    "'TRUSTED','DEGRADED','POOR_QUALITY','BLOCKED','MISSING','QUARANTINED','DERIVED'"
    ")"
)


class OptionChainSnapshot(Base):
    """Point-in-time option chain header record."""

    __tablename__ = "option_chain_snapshot"

    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    underlying_id: Mapped[str] = mapped_column(String(64), nullable=False)
    exchange: Mapped[str] = mapped_column(String(8), nullable=False)
    timestamp: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    expiry: Mapped[datetime.date] = mapped_column(Date(), nullable=False)
    spot_price: Mapped[Optional[float]] = mapped_column(
        Numeric(precision=18, scale=6), nullable=True
    )
    atm_strike: Mapped[Optional[float]] = mapped_column(
        Numeric(precision=18, scale=2), nullable=True
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    source_timestamp: Mapped[Optional[datetime.datetime]] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    received_at: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("NOW()")
    )
    # Derived analytics — NULL when not calculated
    pcr_oi: Mapped[Optional[float]] = mapped_column(
        Numeric(precision=10, scale=4), nullable=True
    )
    pcr_volume: Mapped[Optional[float]] = mapped_column(
        Numeric(precision=10, scale=4), nullable=True
    )
    total_ce_oi: Mapped[Optional[int]] = mapped_column(BigInteger(), nullable=True)
    total_pe_oi: Mapped[Optional[int]] = mapped_column(BigInteger(), nullable=True)
    max_pain: Mapped[Optional[float]] = mapped_column(
        Numeric(precision=18, scale=2), nullable=True
    )
    atm_iv: Mapped[Optional[float]] = mapped_column(
        Numeric(precision=10, scale=4), nullable=True
    )
    quality_status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="TRUSTED"
    )
    session_date: Mapped[Optional[datetime.date]] = mapped_column(
        Date(), nullable=True
    )

    # ── Relationships ─────────────────────────────────────────────────────
    contracts: Mapped[list["OptionChainContract"]] = relationship(
        back_populates="snapshot",
        cascade="all, delete-orphan",
        lazy="select",
    )

    __table_args__ = (
        CheckConstraint(_QUALITY_STATUS_CHECK, name="ocs_quality_status_valid"),
        Index(
            "ocs_underlying_expiry_ts",
            "underlying_id", "expiry", text("timestamp DESC"),
        ),
        Index("ocs_exchange_ts", "exchange", text("timestamp DESC")),
    )

    def __repr__(self) -> str:
        return (
            f"<OptionChainSnapshot underlying={self.underlying_id!r} "
            f"expiry={self.expiry!r} ts={self.timestamp!r}>"
        )


class OptionChainContract(Base):
    """Per-strike row within an OptionChainSnapshot.

    Deleted (CASCADE) when the parent snapshot is deleted.
    CE and PE are stored as separate rows — never combined.
    """

    __tablename__ = "option_chain_contract"

    id: Mapped[int] = mapped_column(BigInteger(), Identity(), primary_key=True)
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    instrument_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    strike: Mapped[float] = mapped_column(
        Numeric(precision=18, scale=2), nullable=False
    )
    option_type: Mapped[str] = mapped_column(
        String(2), nullable=False, comment="CE | PE"
    )

    # ── Market data ───────────────────────────────────────────────────────
    ltp: Mapped[Optional[float]] = mapped_column(
        Numeric(precision=18, scale=6), nullable=True
    )
    open: Mapped[Optional[float]] = mapped_column(
        Numeric(precision=18, scale=6), nullable=True
    )
    high: Mapped[Optional[float]] = mapped_column(
        Numeric(precision=18, scale=6), nullable=True
    )
    low: Mapped[Optional[float]] = mapped_column(
        Numeric(precision=18, scale=6), nullable=True
    )
    close: Mapped[Optional[float]] = mapped_column(
        Numeric(precision=18, scale=6), nullable=True
    )
    volume: Mapped[Optional[int]] = mapped_column(BigInteger(), nullable=True)
    open_interest: Mapped[Optional[int]] = mapped_column(BigInteger(), nullable=True)
    oi_change: Mapped[Optional[int]] = mapped_column(BigInteger(), nullable=True)
    bid: Mapped[Optional[float]] = mapped_column(
        Numeric(precision=18, scale=6), nullable=True
    )
    ask: Mapped[Optional[float]] = mapped_column(
        Numeric(precision=18, scale=6), nullable=True
    )
    bid_quantity: Mapped[Optional[int]] = mapped_column(BigInteger(), nullable=True)
    ask_quantity: Mapped[Optional[int]] = mapped_column(BigInteger(), nullable=True)

    # ── Greeks — NULL when not supplied; zero PROHIBITED ─────────────────
    iv: Mapped[Optional[float]] = mapped_column(
        Numeric(precision=10, scale=4), nullable=True
    )
    delta: Mapped[Optional[float]] = mapped_column(
        Numeric(precision=10, scale=6), nullable=True
    )
    gamma: Mapped[Optional[float]] = mapped_column(
        Numeric(precision=10, scale=8), nullable=True
    )
    theta: Mapped[Optional[float]] = mapped_column(
        Numeric(precision=10, scale=6), nullable=True
    )
    vega: Mapped[Optional[float]] = mapped_column(
        Numeric(precision=10, scale=6), nullable=True
    )
    rho: Mapped[Optional[float]] = mapped_column(
        Numeric(precision=10, scale=6), nullable=True
    )
    intrinsic_value: Mapped[Optional[float]] = mapped_column(
        Numeric(precision=18, scale=6), nullable=True
    )
    time_value: Mapped[Optional[float]] = mapped_column(
        Numeric(precision=18, scale=6), nullable=True
    )
    is_atm: Mapped[bool] = mapped_column(
        Boolean(), nullable=False, server_default="FALSE"
    )

    # ── Relationship ──────────────────────────────────────────────────────
    snapshot: Mapped["OptionChainSnapshot"] = relationship(
        back_populates="contracts",
        foreign_keys=[snapshot_id],
        primaryjoin="OptionChainContract.snapshot_id == OptionChainSnapshot.snapshot_id",
    )

    __table_args__ = (
        CheckConstraint("option_type IN ('CE','PE')", name="occ_option_type_valid"),
        CheckConstraint("strike > 0", name="occ_strike_positive"),
        Index("occ_snapshot_strike_type", "snapshot_id", "strike", "option_type"),
    )

    def __repr__(self) -> str:
        return (
            f"<OptionChainContract strike={self.strike} {self.option_type!r} "
            f"ltp={self.ltp} iv={self.iv}>"
        )


class OptionGreeksSnapshot(Base):
    """IV and Greeks time-series per option instrument.

    TimescaleDB hypertable on ``timestamp`` (1-day chunks, 90-day retention).
    Separate from OptionChainContract to avoid mixing fundamentally different
    observation types (candle vs. snapshot-level Greeks).
    """

    __tablename__ = "option_greeks_snapshot"

    id: Mapped[int] = mapped_column(BigInteger(), Identity(), nullable=False)
    instrument_id: Mapped[str] = mapped_column(String(64), nullable=False)
    timestamp: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    underlying_price: Mapped[Optional[float]] = mapped_column(
        Numeric(precision=18, scale=6), nullable=True
    )
    option_price: Mapped[Optional[float]] = mapped_column(
        Numeric(precision=18, scale=6), nullable=True
    )
    # All Greeks NULL when not available — zero PROHIBITED
    iv: Mapped[Optional[float]] = mapped_column(
        Numeric(precision=10, scale=4), nullable=True
    )
    delta: Mapped[Optional[float]] = mapped_column(
        Numeric(precision=10, scale=6), nullable=True
    )
    gamma: Mapped[Optional[float]] = mapped_column(
        Numeric(precision=10, scale=8), nullable=True
    )
    theta: Mapped[Optional[float]] = mapped_column(
        Numeric(precision=10, scale=6), nullable=True
    )
    vega: Mapped[Optional[float]] = mapped_column(
        Numeric(precision=10, scale=6), nullable=True
    )
    rho: Mapped[Optional[float]] = mapped_column(
        Numeric(precision=10, scale=6), nullable=True
    )
    calculation_method: Mapped[Optional[str]] = mapped_column(
        String(32), nullable=True, comment="BS | BINOMIAL | PROVIDER"
    )
    provider: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    source_timestamp: Mapped[Optional[datetime.datetime]] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    received_at: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("NOW()")
    )
    quality_status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="TRUSTED"
    )
    session_date: Mapped[Optional[datetime.date]] = mapped_column(
        Date(), nullable=True
    )

    __table_args__ = (
        {"primary_key": (id, timestamp)},
        CheckConstraint(_QUALITY_STATUS_CHECK, name="ogs_quality_status_valid"),
        Index("ogs_instrument_timestamp", "instrument_id", text("timestamp DESC")),
    )

    def __repr__(self) -> str:
        return (
            f"<OptionGreeksSnapshot {self.instrument_id!r} "
            f"ts={self.timestamp!r} iv={self.iv}>"
        )
