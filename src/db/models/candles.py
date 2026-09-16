"""
src/db/models/candles.py

SQLAlchemy ORM models for canonical OHLCV candle tables:
  - EquityCandle   — NSE/BSE equities + indices (TimescaleDB hypertable)
  - FuturesCandle  — NSE/BSE F&O futures       (TimescaleDB hypertable)
  - OptionsCandle  — NSE/BSE F&O options       (TimescaleDB hypertable)

All three tables enforce:
  - CHECK (interval_str <> '3m')       — 3m permanently banned
  - OHLC integrity constraints          — high >= open/close, low <= open/close
  - Non-negative volume
  - Standardised quality_status enum
  - Deterministic unique key for idempotent upserts

Requirements: 4.1, 4.2, 10.11
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
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.sqltypes import TIMESTAMP

from src.db.models.base import Base

# ---------------------------------------------------------------------------
# Shared CHECK constraint expressions — keep in sync with Alembic migration
# ---------------------------------------------------------------------------
_INTERVAL_NOT_3M = "interval_str <> '3m'"
_OHLC_HIGH_GTE_OPEN = "high >= open"
_OHLC_HIGH_GTE_CLOSE = "high >= close"
_OHLC_LOW_LTE_OPEN = "low <= open"
_OHLC_LOW_LTE_CLOSE = "low <= close"
_OHLC_HIGH_GTE_LOW = "high >= low"
_QUALITY_STATUS_CHECK = (
    "quality_status IN ("
    "'TRUSTED','DEGRADED','POOR_QUALITY','BLOCKED','MISSING','QUARANTINED','DERIVED'"
    ")"
)
_DATA_ORIGIN_CHECK = "data_origin IN ('PROVIDER', 'DERIVED')"

class EquityCandle(Base):
    """Canonical OHLCV candle for NSE/BSE equities and indices.

    TimescaleDB hypertable partitioned on ``time`` (7-day chunks).
    Segment values: EQ (equity), IDX (index), ETF (exchange-traded fund).

    Business key: (instrument_id, exchange, interval_str, time) — unique.
    """

    __tablename__ = "equity_candle"

    id: Mapped[int] = mapped_column(BigInteger(), Identity(), primary_key=True)
    instrument_id: Mapped[str] = mapped_column(String(64), nullable=False)
    exchange: Mapped[str] = mapped_column(String(8), nullable=False)
    segment: Mapped[str] = mapped_column(
        String(8), nullable=False, server_default="EQ",
        comment="EQ | IDX | ETF",
    )
    interval_str: Mapped[str] = mapped_column(String(4), nullable=False)
    time: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, primary_key=True,
        comment="Candle open timestamp UTC — hypertable partition key",
    )
    session_date: Mapped[datetime.date] = mapped_column(Date(), nullable=False)

    # ── OHLCV ─────────────────────────────────────────────────────────────
    open: Mapped[float] = mapped_column(Numeric(precision=18, scale=6), nullable=False)
    high: Mapped[float] = mapped_column(Numeric(precision=18, scale=6), nullable=False)
    low: Mapped[float] = mapped_column(Numeric(precision=18, scale=6), nullable=False)
    close: Mapped[float] = mapped_column(Numeric(precision=18, scale=6), nullable=False)
    volume: Mapped[int] = mapped_column(
        BigInteger(), nullable=False, server_default="0"
    )
    vwap: Mapped[Optional[float]] = mapped_column(
        Numeric(precision=18, scale=6), nullable=True
    )
    turnover: Mapped[Optional[float]] = mapped_column(
        Numeric(precision=24, scale=4), nullable=True,
        comment="Total traded value INR",
    )

    # ── Provenance ────────────────────────────────────────────────────────
    data_origin: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="PROVIDER",
        comment="PROVIDER | DERIVED",
    )
    derived_from_interval: Mapped[Optional[str]] = mapped_column(
        String(4), nullable=True
    )
    aggregation_version: Mapped[Optional[str]] = mapped_column(
        String(16), nullable=True
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_timestamp: Mapped[Optional[datetime.datetime]] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    received_at: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("NOW()")
    )
    normalisation_version: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="2.0.0"
    )
    dataset_version: Mapped[int] = mapped_column(
        BigInteger(), nullable=False, server_default="1"
    )

    # ── Quality ───────────────────────────────────────────────────────────
    quality_status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="TRUSTED"
    )
    poor_quality: Mapped[bool] = mapped_column(
        Boolean(), nullable=False, server_default="FALSE"
    )
    reconciliation_status: Mapped[Optional[str]] = mapped_column(
        String(32), nullable=True
    )
    provenance_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    volume_unavailable: Mapped[bool] = mapped_column(
        Boolean(), nullable=False, server_default="FALSE"
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("NOW()")
    )

    __table_args__ = (
        UniqueConstraint(
            "instrument_id", "exchange", "interval_str", "time",
            name="equity_candle_uq",
        ),
        # Data integrity
        CheckConstraint(_INTERVAL_NOT_3M, name="ec_no_3m_interval"),
        CheckConstraint(_OHLC_HIGH_GTE_OPEN, name="ec_high_gte_open"),
        CheckConstraint(_OHLC_HIGH_GTE_CLOSE, name="ec_high_gte_close"),
        CheckConstraint(_OHLC_LOW_LTE_OPEN, name="ec_low_lte_open"),
        CheckConstraint(_OHLC_LOW_LTE_CLOSE, name="ec_low_lte_close"),
        CheckConstraint(_OHLC_HIGH_GTE_LOW, name="ec_high_gte_low"),
        CheckConstraint("volume >= 0", name="ec_volume_non_negative"),
        CheckConstraint(_DATA_ORIGIN_CHECK, name="ec_data_origin_valid"),
        CheckConstraint(_QUALITY_STATUS_CHECK, name="ec_quality_status_valid"),
        # Query indexes
        Index(
            "ec_instrument_interval_time",
            "instrument_id", "interval_str", text("time DESC"),
        ),
        Index(
            "ec_exchange_segment_interval_time",
            "exchange", "segment", "interval_str", text("time DESC"),
        ),
        Index(
            "ec_quality_filter",
            "quality_status", "instrument_id", "interval_str", text("time DESC"),
            postgresql_where=text("quality_status <> 'TRUSTED'"),
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<EquityCandle {self.instrument_id!r} {self.interval_str!r} "
            f"t={self.time!r} close={self.close}>"
        )

class FuturesCandle(Base):
    """Canonical OHLCV + OI candle for NSE/BSE F&O futures contracts.

    TimescaleDB hypertable partitioned on ``time`` (7-day chunks).
    Every contract is uniquely identified by instrument_id (which encodes
    the expiry). Separate expiry contracts are NEVER merged.
    """

    __tablename__ = "futures_candle"

    id: Mapped[int] = mapped_column(BigInteger(), Identity(), primary_key=True)
    instrument_id: Mapped[str] = mapped_column(String(64), nullable=False)
    underlying_id: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True,
        comment="instrument_id of the underlying equity or index",
    )
    exchange: Mapped[str] = mapped_column(
        String(8), nullable=False, comment="NFO | BFO"
    )
    interval_str: Mapped[str] = mapped_column(String(4), nullable=False)
    time: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    session_date: Mapped[datetime.date] = mapped_column(Date(), nullable=False)
    expiry: Mapped[datetime.date] = mapped_column(Date(), nullable=False)
    contract_type: Mapped[str] = mapped_column(
        String(8), nullable=False, server_default="FUT"
    )

    # ── OHLCV + OI ────────────────────────────────────────────────────────
    open: Mapped[float] = mapped_column(Numeric(precision=18, scale=6), nullable=False)
    high: Mapped[float] = mapped_column(Numeric(precision=18, scale=6), nullable=False)
    low: Mapped[float] = mapped_column(Numeric(precision=18, scale=6), nullable=False)
    close: Mapped[float] = mapped_column(Numeric(precision=18, scale=6), nullable=False)
    volume: Mapped[int] = mapped_column(
        BigInteger(), nullable=False, server_default="0"
    )
    open_interest: Mapped[Optional[int]] = mapped_column(
        BigInteger(), nullable=True
    )
    oi_change: Mapped[Optional[int]] = mapped_column(BigInteger(), nullable=True)
    vwap: Mapped[Optional[float]] = mapped_column(
        Numeric(precision=18, scale=6), nullable=True
    )
    turnover: Mapped[Optional[float]] = mapped_column(
        Numeric(precision=24, scale=4), nullable=True
    )

    # ── Provenance ────────────────────────────────────────────────────────
    data_origin: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="PROVIDER"
    )
    derived_from_interval: Mapped[Optional[str]] = mapped_column(
        String(4), nullable=True
    )
    aggregation_version: Mapped[Optional[str]] = mapped_column(
        String(16), nullable=True
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_timestamp: Mapped[Optional[datetime.datetime]] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    received_at: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("NOW()")
    )
    normalisation_version: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="2.0.0"
    )
    dataset_version: Mapped[int] = mapped_column(
        BigInteger(), nullable=False, server_default="1"
    )

    # ── Quality ───────────────────────────────────────────────────────────
    quality_status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="TRUSTED"
    )
    poor_quality: Mapped[bool] = mapped_column(
        Boolean(), nullable=False, server_default="FALSE"
    )
    reconciliation_status: Mapped[Optional[str]] = mapped_column(
        String(32), nullable=True
    )
    provenance_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("NOW()")
    )

    __table_args__ = (
        UniqueConstraint(
            "instrument_id", "exchange", "interval_str", "time",
            name="futures_candle_uq",
        ),
        CheckConstraint(_INTERVAL_NOT_3M, name="fc_no_3m_interval"),
        CheckConstraint(_OHLC_HIGH_GTE_OPEN, name="fc_high_gte_open"),
        CheckConstraint(_OHLC_HIGH_GTE_CLOSE, name="fc_high_gte_close"),
        CheckConstraint(_OHLC_LOW_LTE_OPEN, name="fc_low_lte_open"),
        CheckConstraint(_OHLC_LOW_LTE_CLOSE, name="fc_low_lte_close"),
        CheckConstraint(_OHLC_HIGH_GTE_LOW, name="fc_high_gte_low"),
        CheckConstraint("volume >= 0", name="fc_volume_non_negative"),
        CheckConstraint(
            "open_interest IS NULL OR open_interest >= 0",
            name="fc_oi_non_negative",
        ),
        CheckConstraint("contract_type = 'FUT'", name="fc_contract_type_fut"),
        CheckConstraint(_DATA_ORIGIN_CHECK, name="fc_data_origin_valid"),
        CheckConstraint(_QUALITY_STATUS_CHECK, name="fc_quality_status_valid"),
        Index(
            "fc_instrument_interval_time",
            "instrument_id", "interval_str", text("time DESC"),
        ),
        Index(
            "fc_underlying_expiry_interval_time",
            "underlying_id", "expiry", "interval_str", text("time DESC"),
            postgresql_where=text("underlying_id IS NOT NULL"),
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<FuturesCandle {self.instrument_id!r} expiry={self.expiry!r} "
            f"{self.interval_str!r} close={self.close}>"
        )

class OptionsCandle(Base):
    """Canonical OHLCV + OI candle for NSE/BSE F&O options contracts.

    TimescaleDB hypertable partitioned on ``time`` (7-day chunks).
    CE and PE are NEVER combined. Different strikes are NEVER merged.
    Different expiries are NEVER merged.
    """

    __tablename__ = "options_candle"

    id: Mapped[int] = mapped_column(BigInteger(), Identity(), primary_key=True)
    instrument_id: Mapped[str] = mapped_column(String(64), nullable=False)
    underlying_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    exchange: Mapped[str] = mapped_column(String(8), nullable=False)
    interval_str: Mapped[str] = mapped_column(String(4), nullable=False)
    time: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    session_date: Mapped[datetime.date] = mapped_column(Date(), nullable=False)
    expiry: Mapped[datetime.date] = mapped_column(Date(), nullable=False)
    strike: Mapped[float] = mapped_column(
        Numeric(precision=18, scale=2), nullable=False
    )
    option_type: Mapped[str] = mapped_column(
        String(2), nullable=False, comment="CE | PE only"
    )

    # ── OHLCV + OI ────────────────────────────────────────────────────────
    open: Mapped[float] = mapped_column(Numeric(precision=18, scale=6), nullable=False)
    high: Mapped[float] = mapped_column(Numeric(precision=18, scale=6), nullable=False)
    low: Mapped[float] = mapped_column(Numeric(precision=18, scale=6), nullable=False)
    close: Mapped[float] = mapped_column(Numeric(precision=18, scale=6), nullable=False)
    volume: Mapped[int] = mapped_column(
        BigInteger(), nullable=False, server_default="0"
    )
    open_interest: Mapped[Optional[int]] = mapped_column(BigInteger(), nullable=True)
    oi_change: Mapped[Optional[int]] = mapped_column(BigInteger(), nullable=True)
    vwap: Mapped[Optional[float]] = mapped_column(
        Numeric(precision=18, scale=6), nullable=True
    )
    turnover: Mapped[Optional[float]] = mapped_column(
        Numeric(precision=24, scale=4), nullable=True
    )

    # ── Provenance ────────────────────────────────────────────────────────
    data_origin: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="PROVIDER"
    )
    derived_from_interval: Mapped[Optional[str]] = mapped_column(
        String(4), nullable=True
    )
    aggregation_version: Mapped[Optional[str]] = mapped_column(
        String(16), nullable=True
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_timestamp: Mapped[Optional[datetime.datetime]] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    received_at: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("NOW()")
    )
    normalisation_version: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="2.0.0"
    )
    dataset_version: Mapped[int] = mapped_column(
        BigInteger(), nullable=False, server_default="1"
    )

    # ── Quality ───────────────────────────────────────────────────────────
    quality_status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="TRUSTED"
    )
    poor_quality: Mapped[bool] = mapped_column(
        Boolean(), nullable=False, server_default="FALSE"
    )
    reconciliation_status: Mapped[Optional[str]] = mapped_column(
        String(32), nullable=True
    )
    provenance_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("NOW()")
    )

    __table_args__ = (
        UniqueConstraint(
            "instrument_id", "exchange", "interval_str", "time",
            name="options_candle_uq",
        ),
        CheckConstraint(_INTERVAL_NOT_3M, name="oc_no_3m_interval"),
        CheckConstraint("option_type IN ('CE','PE')", name="oc_option_type_valid"),
        CheckConstraint("strike > 0", name="oc_strike_positive"),
        CheckConstraint(_OHLC_HIGH_GTE_OPEN, name="oc_high_gte_open"),
        CheckConstraint(_OHLC_HIGH_GTE_CLOSE, name="oc_high_gte_close"),
        CheckConstraint(_OHLC_LOW_LTE_OPEN, name="oc_low_lte_open"),
        CheckConstraint(_OHLC_LOW_LTE_CLOSE, name="oc_low_lte_close"),
        CheckConstraint(_OHLC_HIGH_GTE_LOW, name="oc_high_gte_low"),
        CheckConstraint("volume >= 0", name="oc_volume_non_negative"),
        CheckConstraint(
            "open_interest IS NULL OR open_interest >= 0",
            name="oc_oi_non_negative",
        ),
        CheckConstraint(_DATA_ORIGIN_CHECK, name="oc_data_origin_valid"),
        CheckConstraint(_QUALITY_STATUS_CHECK, name="oc_quality_status_valid"),
        Index(
            "oc_instrument_interval_time",
            "instrument_id", "interval_str", text("time DESC"),
        ),
        Index(
            "oc_underlying_expiry_strike_type",
            "underlying_id", "expiry", "strike", "option_type",
            "interval_str", text("time DESC"),
            postgresql_where=text("underlying_id IS NOT NULL"),
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<OptionsCandle {self.instrument_id!r} expiry={self.expiry!r} "
            f"strike={self.strike} {self.option_type!r} close={self.close}>"
        )
