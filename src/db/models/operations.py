"""
src/db/models/operations.py

SQLAlchemy ORM models for ingestion operations infrastructure:
  - IngestionJob         — backfill/ingestion job tracking with row counters
  - IngestionCheckpoint  — resumable job state per (provider, dataset, instrument)
  - CandleBarQuarantine  — migration quarantine for unclassifiable candle_bar rows
"""
from __future__ import annotations

import datetime
import uuid
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql.sqltypes import TIMESTAMP

from src.db.models.base import Base


class IngestionJob(Base):
    """Tracks a single backfill, live ingestion, reconciliation, or gap-recovery job.

    Row counters provide full accountability:
      received = inserted + updated + skipped + invalid + duplicate + failed + quarantine
    """

    __tablename__ = "ingestion_job"

    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    job_type: Mapped[str] = mapped_column(
        String(32), nullable=False,
        comment="BACKFILL | LIVE | RECONCILE | GAP_RECOVERY",
    )
    dataset: Mapped[str] = mapped_column(
        String(32), nullable=False,
        comment="EQUITY_CANDLE | FUTURES_CANDLE | OPTIONS_CANDLE | etc.",
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    instrument_id: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True
    )
    exchange: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    interval_str: Mapped[Optional[str]] = mapped_column(String(4), nullable=True)
    start_time: Mapped[Optional[datetime.datetime]] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    end_time: Mapped[Optional[datetime.datetime]] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )

    # ── Status ────────────────────────────────────────────────────────────
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="PENDING",
        comment="PENDING | RUNNING | COMPLETED | FAILED | CANCELLED",
    )

    # ── Row counters ──────────────────────────────────────────────────────
    requested_rows: Mapped[Optional[int]] = mapped_column(Integer(), nullable=True)
    received_rows: Mapped[Optional[int]] = mapped_column(Integer(), nullable=True)
    inserted_rows: Mapped[Optional[int]] = mapped_column(Integer(), nullable=True)
    updated_rows: Mapped[Optional[int]] = mapped_column(Integer(), nullable=True)
    skipped_rows: Mapped[Optional[int]] = mapped_column(Integer(), nullable=True)
    invalid_rows: Mapped[Optional[int]] = mapped_column(Integer(), nullable=True)
    duplicate_rows: Mapped[Optional[int]] = mapped_column(Integer(), nullable=True)
    failed_rows: Mapped[Optional[int]] = mapped_column(Integer(), nullable=True)
    quarantine_rows: Mapped[Optional[int]] = mapped_column(Integer(), nullable=True)

    # ── Timing ────────────────────────────────────────────────────────────
    started_at: Mapped[Optional[datetime.datetime]] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    completed_at: Mapped[Optional[datetime.datetime]] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    error: Mapped[Optional[str]] = mapped_column(Text(), nullable=True)
    retry_count: Mapped[int] = mapped_column(
        Integer(), nullable=False, server_default="0"
    )
    parent_job_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ingestion_job.job_id", name="ij_parent_job_fk"),
        nullable=True,
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("NOW()")
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("NOW()")
    )

    # ── Self-referential relationship ─────────────────────────────────────
    child_jobs: Mapped[list["IngestionJob"]] = relationship(
        "IngestionJob",
        foreign_keys=[parent_job_id],
        backref="parent_job",
        lazy="select",
    )

    __table_args__ = (
        Index(
            "ij_instrument_dataset_status",
            "instrument_id", "interval_str", "dataset", "status",
            postgresql_where=text("instrument_id IS NOT NULL"),
        ),
        Index(
            "ij_active_jobs", "status", text("created_at DESC"),
            postgresql_where=text("status <> 'COMPLETED'"),
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<IngestionJob job_id={self.job_id!r} type={self.job_type!r} "
            f"dataset={self.dataset!r} status={self.status!r}>"
        )


class IngestionCheckpoint(Base):
    """Resumable checkpoint for backfill and live ingestion jobs.

    One row per (provider, dataset, instrument_id, exchange, interval_str).
    ``last_successful_timestamp`` is the last point successfully persisted;
    resuming a job starts from this timestamp.
    """

    __tablename__ = "ingestion_checkpoint"

    id: Mapped[int] = mapped_column(BigInteger(), Identity(), primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    dataset: Mapped[str] = mapped_column(String(32), nullable=False)
    instrument_id: Mapped[str] = mapped_column(String(64), nullable=False)
    exchange: Mapped[str] = mapped_column(String(8), nullable=False)
    interval_str: Mapped[str] = mapped_column(String(4), nullable=False)
    last_successful_timestamp: Mapped[Optional[datetime.datetime]] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    last_attempted_timestamp: Mapped[Optional[datetime.datetime]] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    last_job_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ingestion_job.job_id", name="ic_last_job_fk"),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="IDLE",
        comment="IDLE | RUNNING | PAUSED | FAILED",
    )
    consecutive_failures: Mapped[int] = mapped_column(
        Integer(), nullable=False, server_default="0"
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("NOW()")
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("NOW()")
    )

    __table_args__ = (
        UniqueConstraint(
            "provider", "dataset", "instrument_id", "exchange", "interval_str",
            name="ic_provider_dataset_instrument_uq",
        ),
        Index(
            "ic_instrument_exchange_interval",
            "instrument_id", "exchange", "interval_str", "dataset",
        ),
        Index(
            "ic_non_idle", "status",
            postgresql_where=text("status <> 'IDLE'"),
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<IngestionCheckpoint {self.provider!r} {self.dataset!r} "
            f"{self.instrument_id!r} {self.interval_str!r} "
            f"last_ts={self.last_successful_timestamp!r}>"
        )


class CandleBarQuarantine(Base):
    """Quarantine table for candle_bar rows that could not be classified.

    During migration, any row that cannot be deterministically classified
    as EQUITY, INDEX, FUTURE, OPTION, or CRYPTO is placed here for
    manual investigation rather than being silently discarded.

    quarantine_status values: PENDING | RESOLVED | DISCARDED
    """

    __tablename__ = "candle_bar_quarantine"

    id: Mapped[int] = mapped_column(BigInteger(), Identity(), primary_key=True)
    original_id: Mapped[int] = mapped_column(
        BigInteger(), nullable=False,
        comment="candle_bar.id of the original row",
    )
    instrument_id: Mapped[str] = mapped_column(String(64), nullable=False)
    exchange: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    interval_str: Mapped[Optional[str]] = mapped_column(String(4), nullable=True)
    time: Mapped[Optional[datetime.datetime]] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    open: Mapped[Optional[float]] = mapped_column(
        __import__("sqlalchemy").Numeric(precision=18, scale=6), nullable=True
    )
    high: Mapped[Optional[float]] = mapped_column(
        __import__("sqlalchemy").Numeric(precision=18, scale=6), nullable=True
    )
    low: Mapped[Optional[float]] = mapped_column(
        __import__("sqlalchemy").Numeric(precision=18, scale=6), nullable=True
    )
    close: Mapped[Optional[float]] = mapped_column(
        __import__("sqlalchemy").Numeric(precision=18, scale=6), nullable=True
    )
    volume: Mapped[Optional[int]] = mapped_column(BigInteger(), nullable=True)
    oi: Mapped[Optional[int]] = mapped_column(BigInteger(), nullable=True)
    provider: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    source_type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    normalisation_version: Mapped[Optional[str]] = mapped_column(
        String(16), nullable=True
    )
    quarantine_reason: Mapped[str] = mapped_column(Text(), nullable=False)
    quarantine_status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="PENDING",
        comment="PENDING | RESOLVED | DISCARDED",
    )
    resolved_to_table: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True
    )
    resolved_at: Mapped[Optional[datetime.datetime]] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("NOW()")
    )

    __table_args__ = (
        Index("cbq_instrument_status", "instrument_id", "quarantine_status"),
    )

    def __repr__(self) -> str:
        return (
            f"<CandleBarQuarantine original_id={self.original_id} "
            f"{self.instrument_id!r} reason={self.quarantine_reason!r}>"
        )
