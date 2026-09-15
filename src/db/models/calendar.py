"""
src/db/models/calendar.py

SQLAlchemy ORM models for calendar and session infrastructure:
  - ExchangeCalendar      — NSE/BSE holiday + trading day registry
  - MarketSession         — actual trading session open/close records
  - FnoUniverseMembership — point-in-time F&O eligibility per instrument

Holidays are NEVER fabricated. Future dates with unknown status use
day_type='NOT_PUBLISHED' and session_status='UNKNOWN'.
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
    String,
    Text,
    Time,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.sqltypes import TIMESTAMP

from src.db.models.base import Base


class ExchangeCalendar(Base):
    """Authoritative exchange trading calendar.

    One row per (exchange, segment, calendar_date).
    day_type values: TRADING_DAY | WEEKEND | OFFICIAL_HOLIDAY |
                     SPECIAL_SESSION | NOT_PUBLISHED | UNKNOWN
    """

    __tablename__ = "exchange_calendar"

    id: Mapped[int] = mapped_column(BigInteger(), Identity(), primary_key=True)
    exchange: Mapped[str] = mapped_column(String(8), nullable=False)
    segment: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    market: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    calendar_date: Mapped[datetime.date] = mapped_column(Date(), nullable=False)
    day_type: Mapped[str] = mapped_column(
        String(32), nullable=False,
        comment="TRADING_DAY|WEEKEND|OFFICIAL_HOLIDAY|SPECIAL_SESSION|NOT_PUBLISHED|UNKNOWN",
    )
    holiday_name: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    holiday_type: Mapped[Optional[str]] = mapped_column(
        String(32), nullable=True,
        comment="NATIONAL | EXCHANGE_SPECIFIC | HALF_DAY",
    )
    is_trading_day: Mapped[bool] = mapped_column(Boolean(), nullable=False)
    session_status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="UNKNOWN",
        comment="OPEN | CLOSED | HALF_DAY | MUHURAT | UNKNOWN",
    )
    session_open: Mapped[Optional[datetime.time]] = mapped_column(
        Time(), nullable=True, comment="IST session open time"
    )
    session_close: Mapped[Optional[datetime.time]] = mapped_column(
        Time(), nullable=True, comment="IST session close time"
    )
    special_session: Mapped[bool] = mapped_column(
        Boolean(), nullable=False, server_default="FALSE"
    )
    is_official: Mapped[bool] = mapped_column(
        Boolean(), nullable=False, server_default="FALSE"
    )
    source: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True,
        comment="NSE_CIRCULAR | NSE_API | MANUAL",
    )
    source_reference: Mapped[Optional[str]] = mapped_column(
        String(256), nullable=True
    )
    year: Mapped[int] = mapped_column(Integer(), nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("NOW()")
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("NOW()")
    )

    __table_args__ = (
        UniqueConstraint(
            "exchange", "segment", "calendar_date",
            name="ec_exchange_segment_date_uq",
        ),
        Index("ecal_exchange_year_trading", "exchange", "year", "is_trading_day"),
        Index(
            "ecal_trading_dates", "calendar_date",
            postgresql_where=text("is_trading_day = TRUE"),
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<ExchangeCalendar {self.exchange!r} {self.calendar_date!r} "
            f"trading={self.is_trading_day} day_type={self.day_type!r}>"
        )


class MarketSession(Base):
    """Actual trading session record for a given exchange + segment + date.

    Separate from ExchangeCalendar: calendar describes expected schedule,
    MarketSession records what actually happened (actual open/close).
    Critical for gap detection and intraday ML.
    """

    __tablename__ = "market_session"

    session_id: Mapped[int] = mapped_column(BigInteger(), Identity(), primary_key=True)
    exchange: Mapped[str] = mapped_column(String(8), nullable=False)
    segment: Mapped[str] = mapped_column(
        String(8), nullable=False, server_default="EQ"
    )
    session_date: Mapped[datetime.date] = mapped_column(Date(), nullable=False)
    session_type: Mapped[str] = mapped_column(
        String(32), nullable=False,
        comment="REGULAR | PRE_OPEN | POST_MARKET | MUHURAT | SPECIAL",
    )
    open_time: Mapped[Optional[datetime.datetime]] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    close_time: Mapped[Optional[datetime.datetime]] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    is_trading_day: Mapped[bool] = mapped_column(Boolean(), nullable=False)
    actual_open: Mapped[Optional[datetime.datetime]] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True,
        comment="Actual first trade timestamp",
    )
    actual_close: Mapped[Optional[datetime.datetime]] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True,
        comment="Actual last trade timestamp",
    )
    source: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text(), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("NOW()")
    )

    __table_args__ = (
        UniqueConstraint(
            "exchange", "segment", "session_date", "session_type",
            name="ms_exchange_segment_date_type_uq",
        ),
        Index("ms_exchange_session_date", "exchange", text("session_date DESC")),
        Index(
            "ms_trading_days", "session_date",
            postgresql_where=text("is_trading_day = TRUE"),
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<MarketSession {self.exchange!r} {self.session_date!r} "
            f"{self.session_type!r} trading={self.is_trading_day}>"
        )


class FnoUniverseMembership(Base):
    """Point-in-time F&O universe eligibility record per instrument.

    Prevents survivorship bias by recording when each instrument entered
    and exited the F&O universe. effective_to=NULL means currently active.
    """

    __tablename__ = "fno_universe_membership"

    id: Mapped[int] = mapped_column(BigInteger(), Identity(), primary_key=True)
    snapshot_id: Mapped[Optional[int]] = mapped_column(
        BigInteger(), nullable=True,
        comment="FK to fno_universe_snapshot.id",
    )
    instrument_id: Mapped[str] = mapped_column(String(64), nullable=False)
    underlying: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    segment: Mapped[str] = mapped_column(
        String(8), nullable=False, server_default="FO"
    )
    effective_from: Mapped[datetime.date] = mapped_column(Date(), nullable=False)
    effective_to: Mapped[Optional[datetime.date]] = mapped_column(
        Date(), nullable=True, comment="NULL = currently active"
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="ACTIVE",
        comment="ACTIVE | REMOVED | SUSPENDED",
    )
    removal_reason: Mapped[Optional[str]] = mapped_column(Text(), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("NOW()")
    )

    __table_args__ = (
        UniqueConstraint(
            "instrument_id", "effective_from",
            name="fum_instrument_effective_from_uq",
        ),
        Index(
            "fum_active_instruments", "instrument_id", "effective_to",
            postgresql_where=text("status = 'ACTIVE'"),
        ),
        Index(
            "fum_underlying", "underlying", "effective_from",
            postgresql_where=text("underlying IS NOT NULL"),
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<FnoUniverseMembership {self.instrument_id!r} "
            f"from={self.effective_from!r} status={self.status!r}>"
        )
