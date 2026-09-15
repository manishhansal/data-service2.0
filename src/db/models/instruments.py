"""
src/db/models/instruments.py

SQLAlchemy ORM models for instrument identity layer:
  - InstrumentMaster          — canonical instrument registry (enhanced)
  - InstrumentProviderMapping — provider-specific token normalisation
  - InstrumentIdentityHistory — symbol/token change audit trail

Requirements: 2.2, 2.3, 2.7, 2.8
"""
from __future__ import annotations

import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    Identity,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql.sqltypes import TIMESTAMP

from src.db.models.base import Base


class InstrumentMaster(Base):
    """Canonical instrument registry.

    Every tradeable instrument in the platform has exactly one row here.
    Provider-specific tokens live in InstrumentProviderMapping.
    """

    __tablename__ = "instrument_master"

    # ── Core identity ─────────────────────────────────────────────────────
    instrument_id: Mapped[str] = mapped_column(
        String(64), primary_key=True, comment="Platform-canonical ID"
    )
    trading_symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    display_symbol: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    name: Mapped[Optional[str]] = mapped_column(
        String(256), nullable=True, comment="Full display name"
    )
    isin: Mapped[Optional[str]] = mapped_column(String(12), nullable=True)

    # ── Classification ────────────────────────────────────────────────────
    exchange: Mapped[str] = mapped_column(String(8), nullable=False)
    segment: Mapped[str] = mapped_column(String(8), nullable=False)
    instrument_type: Mapped[str] = mapped_column(
        String(16), nullable=False,
        comment="EQ | FUTIDX | FUTSTK | OPTIDX | OPTSTK | ETF | IDX",
    )
    instrument_class: Mapped[Optional[str]] = mapped_column(
        String(8), nullable=True,
        comment="EQ | IDX | FUT | OPT | CRYPTO | ETF — coarser than instrument_type",
    )

    # ── Derivative-specific ───────────────────────────────────────────────
    underlying: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    expiry: Mapped[Optional[datetime.date]] = mapped_column(Date(), nullable=True)
    strike: Mapped[Optional[float]] = mapped_column(
        Numeric(precision=18, scale=2), nullable=True
    )
    option_type: Mapped[Optional[str]] = mapped_column(
        String(4), nullable=True, comment="CE | PE | NULL"
    )

    # ── Contract specs ────────────────────────────────────────────────────
    lot_size: Mapped[int] = mapped_column(Integer(), nullable=False, server_default="1")
    tick_size: Mapped[float] = mapped_column(
        Numeric(precision=10, scale=4), nullable=False, server_default="0.0500"
    )

    # ── Lifecycle ─────────────────────────────────────────────────────────
    active_from: Mapped[datetime.date] = mapped_column(Date(), nullable=False)
    active_to: Mapped[Optional[datetime.date]] = mapped_column(Date(), nullable=True)

    # ── Legacy provider tokens (kept for backward compatibility)
    # These are migrated to InstrumentProviderMapping but kept here during
    # the dual-read transition period.
    angel_token: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    angel_symbol: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    upstox_key: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    upstox_symbol: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    # ── Timestamps ────────────────────────────────────────────────────────
    created_at: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("NOW()")
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("NOW()")
    )

    # ── Relationships ─────────────────────────────────────────────────────
    provider_mappings: Mapped[list["InstrumentProviderMapping"]] = relationship(
        back_populates="instrument", lazy="select"
    )
    identity_history: Mapped[list["InstrumentIdentityHistory"]] = relationship(
        back_populates="instrument", lazy="select"
    )

    __table_args__ = (
        Index("im_trading_symbol", "trading_symbol"),
        Index(
            "im_active_instruments", "exchange", "instrument_type",
            postgresql_where=text("active_to IS NULL"),
        ),
        Index(
            "im_underlying_expiry", "underlying", "expiry",
            postgresql_where=text("expiry IS NOT NULL"),
        ),
        Index(
            "im_instrument_class", "instrument_class",
            postgresql_where=text("instrument_class IS NOT NULL"),
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<InstrumentMaster instrument_id={self.instrument_id!r} "
            f"type={self.instrument_type!r} exchange={self.exchange!r}>"
        )


class InstrumentProviderMapping(Base):
    """Provider-specific token/symbol mappings, decoupled from instrument identity.

    One instrument may have multiple active provider mappings (e.g. Angel One
    token AND Upstox key). Each row tracks validity via valid_from/valid_to
    to handle provider reissues and token changes.
    """

    __tablename__ = "instrument_provider_mapping"

    id: Mapped[int] = mapped_column(BigInteger(), Identity(), primary_key=True)
    instrument_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="FK to instrument_master.instrument_id",
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_instrument_id: Mapped[Optional[str]] = mapped_column(
        String(128), nullable=True,
        comment="Provider's native token/ID (e.g. Angel One '2885')",
    )
    provider_symbol: Mapped[Optional[str]] = mapped_column(
        String(128), nullable=True
    )
    exchange_segment: Mapped[Optional[str]] = mapped_column(
        String(32), nullable=True,
        comment="Provider-specific segment string (e.g. 'NSE_EQ|INE002A01018')",
    )
    valid_from: Mapped[datetime.date] = mapped_column(Date(), nullable=False)
    valid_to: Mapped[Optional[datetime.date]] = mapped_column(
        Date(), nullable=True, comment="NULL = currently active"
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean(), nullable=False, server_default="TRUE"
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("NOW()")
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("NOW()")
    )

    # ── Relationship ──────────────────────────────────────────────────────
    instrument: Mapped["InstrumentMaster"] = relationship(
        back_populates="provider_mappings",
        foreign_keys=[instrument_id],
        primaryjoin="InstrumentProviderMapping.instrument_id == InstrumentMaster.instrument_id",
        lazy="select",
    )

    __table_args__ = (
        UniqueConstraint(
            "instrument_id", "provider", "valid_from",
            name="ipm_instrument_provider_valid_from_uq",
        ),
        Index("ipm_provider_token", "provider", "provider_instrument_id"),
        Index(
            "ipm_active_by_instrument", "instrument_id", "provider",
            postgresql_where=text("is_active = TRUE"),
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<InstrumentProviderMapping instrument_id={self.instrument_id!r} "
            f"provider={self.provider!r} token={self.provider_instrument_id!r}>"
        )


class InstrumentIdentityHistory(Base):
    """Audit trail for instrument symbol/token changes and lifecycle events."""

    __tablename__ = "instrument_identity_history"

    id: Mapped[int] = mapped_column(BigInteger(), Identity(), primary_key=True)
    instrument_id: Mapped[str] = mapped_column(String(64), nullable=False)
    old_symbol: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    new_symbol: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    exchange: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    segment: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    change_type: Mapped[str] = mapped_column(
        String(32), nullable=False,
        comment="SYMBOL_CHANGE | TOKEN_CHANGE | CORPORATE_ACTION | EXPIRY | DELISTED",
    )
    valid_from: Mapped[datetime.date] = mapped_column(Date(), nullable=False)
    valid_to: Mapped[Optional[datetime.date]] = mapped_column(Date(), nullable=True)
    reason: Mapped[Optional[str]] = mapped_column(Text(), nullable=True)
    source: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("NOW()")
    )

    # ── Relationship ──────────────────────────────────────────────────────
    instrument: Mapped["InstrumentMaster"] = relationship(
        back_populates="identity_history",
        foreign_keys=[instrument_id],
        primaryjoin="InstrumentIdentityHistory.instrument_id == InstrumentMaster.instrument_id",
        lazy="select",
    )

    __table_args__ = (
        Index("iih_instrument_id", "instrument_id", text("valid_from DESC")),
    )

    def __repr__(self) -> str:
        return (
            f"<InstrumentIdentityHistory instrument_id={self.instrument_id!r} "
            f"change_type={self.change_type!r} valid_from={self.valid_from!r}>"
        )
