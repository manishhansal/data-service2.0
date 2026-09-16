"""
src/db/models/live.py

SQLAlchemy ORM models for live market data:
  - MarketTick   — WebSocket tick records   (TimescaleDB, 1-day chunks, 7-day retention)
  - MarketQuote  — Quote snapshot records   (TimescaleDB, 1-day chunks, 30-day retention)

Null semantics (non-negotiable):
  - bid/ask: NULL when absent — zero is NOT a substitute
  - open_interest: NULL when absent
  - Greeks: not stored here — use OptionGreeksSnapshot
"""
from __future__ import annotations

import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    Identity,
    Index,
    Numeric,
    String,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.sqltypes import TIMESTAMP

from src.db.models.base import Base

_QUALITY_STATUS_CHECK = (
    "quality_status IN ("
    "'TRUSTED','DEGRADED','POOR_QUALITY','BLOCKED','MISSING','QUARANTINED','DERIVED'"
    ")"
)

class MarketTick(Base):
    """Live WebSocket tick record.

    TimescaleDB hypertable on ``timestamp`` (1-day chunks).
    Retention policy: 7 days raw ticks; aggregate into equity_candle after.

    Null semantics: bid, ask, OI, quantities are NULL when not supplied.
    Zero is NEVER used as a substitute for a missing value.
    """

    __tablename__ = "market_tick"

    tick_id: Mapped[int] = mapped_column(BigInteger(), Identity(), primary_key=True)
    instrument_id: Mapped[str] = mapped_column(String(64), nullable=False)
    exchange: Mapped[str] = mapped_column(String(8), nullable=False)
    timestamp: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, primary_key=True,
        comment="Exchange-side tick timestamp — hypertable partition key",
    )
    received_at: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("NOW()")
    )

    # ── Price / volume ────────────────────────────────────────────────────
    ltp: Mapped[Optional[float]] = mapped_column(
        Numeric(precision=18, scale=6), nullable=True
    )
    open: Mapped[Optional[float]] = mapped_column(
        Numeric(precision=18, scale=6), nullable=True, comment="Day open"
    )
    high: Mapped[Optional[float]] = mapped_column(
        Numeric(precision=18, scale=6), nullable=True, comment="Day high"
    )
    low: Mapped[Optional[float]] = mapped_column(
        Numeric(precision=18, scale=6), nullable=True, comment="Day low"
    )
    close: Mapped[Optional[float]] = mapped_column(
        Numeric(precision=18, scale=6), nullable=True,
        comment="Previous session close",
    )
    volume: Mapped[Optional[int]] = mapped_column(BigInteger(), nullable=True)
    open_interest: Mapped[Optional[int]] = mapped_column(BigInteger(), nullable=True)

    # ── Bid/ask — NULL when absent; zero PROHIBITED ───────────────────────
    bid: Mapped[Optional[float]] = mapped_column(
        Numeric(precision=18, scale=6), nullable=True
    )
    ask: Mapped[Optional[float]] = mapped_column(
        Numeric(precision=18, scale=6), nullable=True
    )
    bid_quantity: Mapped[Optional[int]] = mapped_column(BigInteger(), nullable=True)
    ask_quantity: Mapped[Optional[int]] = mapped_column(BigInteger(), nullable=True)
    last_traded_quantity: Mapped[Optional[int]] = mapped_column(
        BigInteger(), nullable=True
    )
    total_buy_quantity: Mapped[Optional[int]] = mapped_column(
        BigInteger(), nullable=True
    )
    total_sell_quantity: Mapped[Optional[int]] = mapped_column(
        BigInteger(), nullable=True
    )

    # ── Metadata ──────────────────────────────────────────────────────────
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    sequence_number: Mapped[Optional[int]] = mapped_column(BigInteger(), nullable=True)
    source_timestamp: Mapped[Optional[datetime.datetime]] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    quality_status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="TRUSTED"
    )
    session_date: Mapped[Optional[datetime.date]] = mapped_column(
        Date(), nullable=True
    )

    __table_args__ = (
        CheckConstraint(_QUALITY_STATUS_CHECK, name="mt_quality_status_valid"),
        Index(
            "mt_instrument_timestamp",
            "instrument_id", text("timestamp DESC"),
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<MarketTick {self.instrument_id!r} ts={self.timestamp!r} "
            f"ltp={self.ltp}>"
        )

class MarketQuote(Base):
    """Live quote snapshot (REST polling or full-quote WebSocket message).

    TimescaleDB hypertable on ``timestamp`` (1-day chunks).
    Retention policy: 30 days.

    Distinct from MarketTick: a quote represents the full market depth
    snapshot at a point in time, not just the last traded price.
    """

    __tablename__ = "market_quote"

    quote_id: Mapped[int] = mapped_column(BigInteger(), Identity(), primary_key=True)
    instrument_id: Mapped[str] = mapped_column(String(64), nullable=False)
    exchange: Mapped[str] = mapped_column(String(8), nullable=False)
    timestamp: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    received_at: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("NOW()")
    )

    # ── Quote fields ──────────────────────────────────────────────────────
    ltp: Mapped[Optional[float]] = mapped_column(
        Numeric(precision=18, scale=6), nullable=True
    )
    ltq: Mapped[Optional[int]] = mapped_column(BigInteger(), nullable=True)
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
    bid: Mapped[Optional[float]] = mapped_column(
        Numeric(precision=18, scale=6), nullable=True
    )
    ask: Mapped[Optional[float]] = mapped_column(
        Numeric(precision=18, scale=6), nullable=True
    )
    bid_quantity: Mapped[Optional[int]] = mapped_column(BigInteger(), nullable=True)
    ask_quantity: Mapped[Optional[int]] = mapped_column(BigInteger(), nullable=True)
    total_buy_quantity: Mapped[Optional[int]] = mapped_column(
        BigInteger(), nullable=True
    )
    total_sell_quantity: Mapped[Optional[int]] = mapped_column(
        BigInteger(), nullable=True
    )
    upper_circuit: Mapped[Optional[float]] = mapped_column(
        Numeric(precision=18, scale=6), nullable=True
    )
    lower_circuit: Mapped[Optional[float]] = mapped_column(
        Numeric(precision=18, scale=6), nullable=True
    )
    week_high_52: Mapped[Optional[float]] = mapped_column(
        Numeric(precision=18, scale=6), nullable=True
    )
    week_low_52: Mapped[Optional[float]] = mapped_column(
        Numeric(precision=18, scale=6), nullable=True
    )

    # ── Metadata ──────────────────────────────────────────────────────────
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    source_timestamp: Mapped[Optional[datetime.datetime]] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    quality_status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="TRUSTED"
    )
    session_date: Mapped[Optional[datetime.date]] = mapped_column(
        Date(), nullable=True
    )

    __table_args__ = (
        CheckConstraint(_QUALITY_STATUS_CHECK, name="mq_quality_status_valid"),
        Index(
            "mq_instrument_timestamp",
            "instrument_id", text("timestamp DESC"),
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<MarketQuote {self.instrument_id!r} ts={self.timestamp!r} "
            f"ltp={self.ltp}>"
        )
