"""
src/db/models/fo_universe.py

Master registry of all NSE F&O eligible instruments (stocks + indices)
over the last 5+ years — the single source of truth for backfill priority
and universe completeness checks.

Every instrument that has ever had active F&O contracts on NSE gets one row.
Delisted/removed instruments are retained with a non-null ``delisted_date``
so historical backfills can still cover the correct date window.

Priority ordering for backfill:
    1 → Index futures underlyings  (NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY)
    2 → Stock futures underlyings  (all F&O-eligible equities)
    3 → NSE broad indices          (INDIA VIX, sectoral indices, etc.)
    4 → Nifty 50 equities          (already in the primary backfill universe)
    5 → Other equities             (remaining NSE equities not in F&O)

The backfill script reads this table (ordered by ``backfill_priority``) so
the most important instruments are always processed first regardless of
changes to the hardcoded Python lists in ``india_instruments.py``.
"""

from __future__ import annotations

import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    Identity,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.sqltypes import TIMESTAMP

from src.db.models.base import Base


class FoUniverse(Base):
    """Master F&O universe instrument registry.

    One row per NSE trading symbol that has ever been F&O-eligible.
    Indices that underlie F&O contracts (NIFTY, BANKNIFTY, etc.) are also
    included here, not just equities.

    Columns
    -------
    instrument_id       Platform-canonical ID  e.g. ``NSE:RELIANCE``.
    symbol              NSE trading symbol     e.g. ``RELIANCE``.
    company_name        Full legal name        e.g. ``Reliance Industries Limited``.
    isin                SEBI ISIN (12 chars)   e.g. ``INE002A01018``.
                        NULL for indices (they don't have ISINs).
    exchange            Exchange segment        ``NSE`` for equities/indices.
    instrument_class    Coarse class           ``EQ`` | ``IDX`` | ``FO``.
    instrument_type     NSE instrument type    ``EQ`` | ``FUTSTK`` | ``FUTIDX`` | ``IDX`` | ``ETF``.
    sector              Broad sector label     e.g. ``Financials`` | ``Energy`` | ``IT``.
    is_index            True for index underlyings (NIFTY, BANKNIFTY, etc.).
    backfill_priority   Lower = higher priority.
                        1=Index futures, 2=Stock futures, 3=Broad indices, 4=Nifty50, 5=Other.
    fo_listed_date      Date the instrument was first admitted to F&O segment.
                        NULL if exact date unknown.
    fo_delisted_date    Date the instrument was removed from F&O segment.
                        NULL = currently active.
    is_fo_active        True if currently in the F&O segment (has active contracts).
    spot_listed_date    Date the equity was first listed on NSE cash market.
    spot_delisted_date  Date the equity was delisted/merged. NULL = still listed.
    is_spot_active      True if the spot equity is still tradeable on NSE.
    successor_symbol    For merged/renamed instruments, the symbol of the successor.
                        e.g. ``CADILAHC`` → ``ZYDUSLIFE``.
    lot_size            Current F&O contract lot size.
    upstox_key          Upstox V3 instrument key  e.g. ``NSE_EQ|INE002A01018``.
    angel_token         Angel One numeric token  e.g. ``2885``.
    yahoo_symbol        Yahoo Finance ticker  e.g. ``RELIANCE.NS``.
                        Useful for the 1d fallback via yfinance.
    notes               Free-text notes about mergers, ticker changes, etc.
    """

    __tablename__ = "fo_universe"

    id: Mapped[int] = mapped_column(
        BigInteger(), Identity(), primary_key=True
    )

    # ── Identity ──────────────────────────────────────────────────────────
    instrument_id: Mapped[str] = mapped_column(
        String(64), nullable=False,
        comment="Canonical ID: exchange:symbol  e.g. NSE:RELIANCE",
    )
    symbol: Mapped[str] = mapped_column(
        String(64), nullable=False,
        comment="NSE trading symbol  e.g. RELIANCE",
    )
    company_name: Mapped[Optional[str]] = mapped_column(
        String(256), nullable=True,
        comment="Full company / index name",
    )
    isin: Mapped[Optional[str]] = mapped_column(
        String(12), nullable=True,
        comment="SEBI ISIN — NULL for indices",
    )

    # ── Classification ────────────────────────────────────────────────────
    exchange: Mapped[str] = mapped_column(
        String(8), nullable=False, server_default="NSE"
    )
    instrument_class: Mapped[str] = mapped_column(
        String(8), nullable=False,
        comment="EQ | IDX | FO",
    )
    instrument_type: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="EQ",
        comment="EQ | IDX | FUTSTK | FUTIDX | ETF",
    )
    sector: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True,
        comment="Broad sector label e.g. Financials, Energy, IT",
    )
    is_index: Mapped[bool] = mapped_column(
        Boolean(), nullable=False, server_default="FALSE",
        comment="True for index underlyings (NIFTY, BANKNIFTY etc.)",
    )

    # ── Backfill priority ─────────────────────────────────────────────────
    backfill_priority: Mapped[int] = mapped_column(
        Integer(), nullable=False, server_default="5",
        comment=(
            "Backfill order: 1=IndexFutures 2=StockFutures "
            "3=BroadIndices 4=Nifty50 5=Other"
        ),
    )

    # ── F&O eligibility window ────────────────────────────────────────────
    fo_listed_date: Mapped[Optional[datetime.date]] = mapped_column(
        Date(), nullable=True,
        comment="Date first admitted to F&O segment; NULL if unknown",
    )
    fo_delisted_date: Mapped[Optional[datetime.date]] = mapped_column(
        Date(), nullable=True,
        comment="Date removed from F&O; NULL = currently active",
    )
    is_fo_active: Mapped[bool] = mapped_column(
        Boolean(), nullable=False, server_default="TRUE",
        comment="True if instrument currently has active F&O contracts",
    )

    # ── Spot (cash market) window ─────────────────────────────────────────
    spot_listed_date: Mapped[Optional[datetime.date]] = mapped_column(
        Date(), nullable=True,
        comment="Date first listed on NSE cash market",
    )
    spot_delisted_date: Mapped[Optional[datetime.date]] = mapped_column(
        Date(), nullable=True,
        comment="Date delisted / merged; NULL = still listed",
    )
    is_spot_active: Mapped[bool] = mapped_column(
        Boolean(), nullable=False, server_default="TRUE",
        comment="True if spot equity is currently tradeable on NSE",
    )

    # ── Corporate actions / succession ───────────────────────────────────
    successor_symbol: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True,
        comment="For merged/renamed instruments, the new symbol e.g. CADILAHC→ZYDUSLIFE",
    )
    notes: Mapped[Optional[str]] = mapped_column(
        Text(), nullable=True,
        comment="Free-text: merger details, ticker changes, ban periods, etc.",
    )

    # ── Contract specs ────────────────────────────────────────────────────
    lot_size: Mapped[Optional[int]] = mapped_column(
        Integer(), nullable=True,
        comment="F&O contract lot size (current; may change at NSE reviews)",
    )

    # ── Provider keys ─────────────────────────────────────────────────────
    upstox_key: Mapped[Optional[str]] = mapped_column(
        String(128), nullable=True,
        comment="Upstox V3 instrument key e.g. NSE_EQ|INE002A01018",
    )
    angel_token: Mapped[Optional[str]] = mapped_column(
        String(32), nullable=True,
        comment="Angel One numeric scrip token e.g. 2885",
    )
    yahoo_symbol: Mapped[Optional[str]] = mapped_column(
        String(32), nullable=True,
        comment="Yahoo Finance ticker e.g. RELIANCE.NS (used as 1d fallback)",
    )

    # ── Timestamps ────────────────────────────────────────────────────────
    created_at: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("NOW()")
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("NOW()")
    )

    # ── Constraints & indexes ─────────────────────────────────────────────
    __table_args__ = (
        # Unique per symbol on this exchange
        UniqueConstraint("symbol", "exchange", name="fou_symbol_exchange_uq"),

        # Validation
        CheckConstraint(
            "instrument_class IN ('EQ','IDX','FO','ETF')",
            name="fou_instrument_class_valid",
        ),
        CheckConstraint(
            "backfill_priority BETWEEN 1 AND 10",
            name="fou_priority_range",
        ),
        CheckConstraint(
            "fo_delisted_date IS NULL OR fo_delisted_date >= fo_listed_date",
            name="fou_fo_dates_consistent",
        ),

        # Core query patterns
        Index("fou_priority_active", "backfill_priority", "is_fo_active"),
        Index("fou_instrument_class", "instrument_class", "backfill_priority"),
        Index("fou_fo_active", "is_fo_active",
              postgresql_where=text("is_fo_active = TRUE")),
        Index("fou_isin", "isin",
              postgresql_where=text("isin IS NOT NULL")),
        Index("fou_instrument_id", "instrument_id"),
    )

    def __repr__(self) -> str:
        status = "active" if self.is_fo_active else f"delisted {self.fo_delisted_date}"
        return (
            f"<FoUniverse {self.symbol!r} [{self.instrument_class}] "
            f"priority={self.backfill_priority} {status}>"
        )
