"""
src/db/models/reconciliation.py

SQLAlchemy ORM models for provider reconciliation and data quality incidents:
  - MarketDepth           — High-frequency depth level storage (TimescaleDB, 1-day retention)
  - ReconciliationRecord  — Per-instrument provider agreement/disagreement records
  - DataIncident          — Durable data gap and quality incident log
  - ClosingAuctionSnapshot — CAS indicative price/quantity (separate from LTP)

Design contracts
----------------
* MarketDepth rows are ephemeral — 1-day retention default.  They are NOT
  aggregated into permanent tables.  Store in TimescaleDB with aggressive
  compression.

* ReconciliationRecord stores both provider observations and the derived
  canonical observation + classification.  Neither provider's value is
  silently discarded.

* DataIncident is durable — never truncated.  Incidents remain for post-
  mortems and ML training data quality audits.

* ClosingAuctionSnapshot MUST NOT contain normal LTP data.  CAS indicative
  prices are categorically different from traded prices and must never be
  used as LTP by any downstream consumer.

Null semantics: all optional price/quantity fields are NULL when absent.
Zero MUST NOT be used as a substitute.

Requirements: Phases 8, 26, 33, 34
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
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.sqltypes import TIMESTAMP

from src.db.models.base import Base

_QUALITY_STATUS_CHECK = (
    "quality_status IN ("
    "'TRUSTED','DEGRADED','POOR_QUALITY','BLOCKED','MISSING','QUARANTINED','DERIVED'"
    ")"
)

_RECON_CLASS_CHECK = (
    "classification IN ("
    "'MATCH','MINOR_DIFFERENCE','SIGNIFICANT_DIFFERENCE','STALE','MISSING','CONFLICT'"
    ")"
)

_INCIDENT_TYPE_CHECK = (
    "incident_type IN ("
    "'DATA_GAP','PROVIDER_DISAGREEMENT','WEBSOCKET_SILENCE','TIMESTAMP_ANOMALY',"
    "'QUALITY_DEGRADATION','PROVIDER_FAILURE','SCHEMA_VIOLATION','OI_ANOMALY'"
    ")"
)

_INCIDENT_STATUS_CHECK = (
    "status IN ('OPEN','RESOLVED','SUPPRESSED','INVESTIGATING')"
)

class MarketDepth(Base):
    """Timestamped market depth record (best-5 or best-30 levels).

    TimescaleDB hypertable on ``timestamp`` (1-day chunks).
    Retention policy: 1 day (raw depth is high-frequency ephemeral data).
    Aggregated depth summaries should be derived separately.

    Each row represents one depth level from one provider at one timestamp.
    To reconstruct a full depth snapshot, query all rows for the same
    (instrument_id, provider, timestamp).

    NOTE: depth-30 rows come from Upstox full_d30 (Upstox Plus plan).
    Do not mix depth-5 and depth-30 rows in the same analysis window.
    """

    __tablename__ = "market_depth"

    depth_id: Mapped[int] = mapped_column(BigInteger(), Identity(), primary_key=True)
    instrument_id: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    exchange: Mapped[str] = mapped_column(String(8), nullable=False)
    timestamp: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, primary_key=True,
        comment="Exchange-side depth timestamp — hypertable partition key",
    )
    received_at: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("NOW()")
    )

    # ── Depth level ───────────────────────────────────────────────────────
    side: Mapped[str] = mapped_column(
        String(4), nullable=False, comment="BUY | SELL"
    )
    level: Mapped[int] = mapped_column(
        BigInteger(), nullable=False, comment="1 = best, 30 = worst"
    )
    price: Mapped[Optional[float]] = mapped_column(
        Numeric(precision=18, scale=6), nullable=True
    )
    quantity: Mapped[Optional[int]] = mapped_column(BigInteger(), nullable=True)
    orders: Mapped[Optional[int]] = mapped_column(BigInteger(), nullable=True)

    # ── Metadata ──────────────────────────────────────────────────────────
    depth_type: Mapped[str] = mapped_column(
        String(8), nullable=False, server_default="D5",
        comment="D5 = 5-level, D30 = 30-level (Upstox Plus)",
    )
    session_date: Mapped[Optional[datetime.date]] = mapped_column(
        Date(), nullable=True
    )

    __table_args__ = (
        CheckConstraint("side IN ('BUY','SELL')", name="md_side_valid"),
        CheckConstraint("level BETWEEN 1 AND 30", name="md_level_valid"),
        CheckConstraint("depth_type IN ('D5','D30')", name="md_depth_type_valid"),
        CheckConstraint(
            "price IS NULL OR price >= 0",
            name="md_price_non_negative",
        ),
        Index(
            "md_instrument_provider_timestamp",
            "instrument_id", "provider", text("timestamp DESC"),
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<MarketDepth {self.instrument_id!r} {self.side} L{self.level} "
            f"ts={self.timestamp!r} price={self.price}>"
        )

class ReconciliationRecord(Base):
    """Provider agreement/disagreement record for a given instrument+timestamp.

    When both Angel One and Upstox provide data for the same instrument,
    the reconciliation engine compares their observations and stores the
    result here.  Neither provider's observation is discarded — both are
    stored so the disagreement is auditable.

    classification values:
      MATCH               — both providers agree within tolerance
      MINOR_DIFFERENCE    — difference < 0.1% or < 1 tick
      SIGNIFICANT_DIFFERENCE — difference >= 0.5% or >= 5 ticks
      STALE               — one provider timestamp significantly older
      MISSING             — data present from only one provider
      CONFLICT            — irreconcilable disagreement (e.g. OI direction)

    The canonical_value is derived from documented routing rules, not
    from arbitrary "the first provider wins" logic.
    """

    __tablename__ = "reconciliation_record"

    record_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    instrument_id: Mapped[str] = mapped_column(String(64), nullable=False)
    exchange: Mapped[str] = mapped_column(String(8), nullable=False)
    reconciled_at: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("NOW()")
    )
    observation_timestamp: Mapped[Optional[datetime.datetime]] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True,
        comment="Timestamp of the data being reconciled",
    )

    # ── Provider A observations ───────────────────────────────────────────
    provider_a: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    provider_a_ltp: Mapped[Optional[float]] = mapped_column(
        Numeric(precision=18, scale=6), nullable=True
    )
    provider_a_oi: Mapped[Optional[int]] = mapped_column(BigInteger(), nullable=True)
    provider_a_volume: Mapped[Optional[int]] = mapped_column(BigInteger(), nullable=True)
    provider_a_bid: Mapped[Optional[float]] = mapped_column(
        Numeric(precision=18, scale=6), nullable=True
    )
    provider_a_ask: Mapped[Optional[float]] = mapped_column(
        Numeric(precision=18, scale=6), nullable=True
    )
    provider_a_iv: Mapped[Optional[float]] = mapped_column(
        Numeric(precision=10, scale=4), nullable=True
    )
    provider_a_timestamp: Mapped[Optional[datetime.datetime]] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )

    # ── Provider B observations ───────────────────────────────────────────
    provider_b: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    provider_b_ltp: Mapped[Optional[float]] = mapped_column(
        Numeric(precision=18, scale=6), nullable=True
    )
    provider_b_oi: Mapped[Optional[int]] = mapped_column(BigInteger(), nullable=True)
    provider_b_volume: Mapped[Optional[int]] = mapped_column(BigInteger(), nullable=True)
    provider_b_bid: Mapped[Optional[float]] = mapped_column(
        Numeric(precision=18, scale=6), nullable=True
    )
    provider_b_ask: Mapped[Optional[float]] = mapped_column(
        Numeric(precision=18, scale=6), nullable=True
    )
    provider_b_iv: Mapped[Optional[float]] = mapped_column(
        Numeric(precision=10, scale=4), nullable=True
    )
    provider_b_timestamp: Mapped[Optional[datetime.datetime]] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )

    # ── Differences ───────────────────────────────────────────────────────
    ltp_diff_abs: Mapped[Optional[float]] = mapped_column(
        Numeric(precision=18, scale=6), nullable=True
    )
    ltp_diff_pct: Mapped[Optional[float]] = mapped_column(
        Numeric(precision=10, scale=6), nullable=True
    )
    oi_diff_abs: Mapped[Optional[int]] = mapped_column(BigInteger(), nullable=True)
    iv_diff_abs: Mapped[Optional[float]] = mapped_column(
        Numeric(precision=10, scale=4), nullable=True
    )
    timestamp_diff_ms: Mapped[Optional[int]] = mapped_column(BigInteger(), nullable=True)

    # ── Canonical result ──────────────────────────────────────────────────
    classification: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="MATCH"
    )
    canonical_provider: Mapped[Optional[str]] = mapped_column(
        String(32), nullable=True,
        comment="Which provider's value was selected as canonical",
    )
    canonical_ltp: Mapped[Optional[float]] = mapped_column(
        Numeric(precision=18, scale=6), nullable=True
    )
    canonical_oi: Mapped[Optional[int]] = mapped_column(BigInteger(), nullable=True)
    resolution_rule: Mapped[Optional[str]] = mapped_column(
        String(128), nullable=True,
        comment="Which routing rule determined the canonical value",
    )
    session_date: Mapped[Optional[datetime.date]] = mapped_column(
        Date(), nullable=True
    )

    __table_args__ = (
        CheckConstraint(_RECON_CLASS_CHECK, name="rr_classification_valid"),
        Index(
            "rr_instrument_ts",
            "instrument_id", text("observation_timestamp DESC"),
        ),
        Index(
            "rr_classification",
            "classification", text("reconciled_at DESC"),
            postgresql_where=text("classification <> 'MATCH'"),
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<ReconciliationRecord {self.instrument_id!r} "
            f"class={self.classification!r} ts={self.observation_timestamp!r}>"
        )

class DataIncident(Base):
    """Durable record of data gaps, quality events, and provider failures.

    Unlike gap_recovery Redis state (which is ephemeral), DataIncidents
    are permanent.  They are used for:
      - Post-mortem analysis
      - ML training data quality flags
      - SLA reporting
      - Alerting thresholds

    incident_type values:
      DATA_GAP              — missing candles or ticks in expected window
      PROVIDER_DISAGREEMENT — reconciliation conflict above threshold
      WEBSOCKET_SILENCE     — WebSocket received no ticks > N seconds
      TIMESTAMP_ANOMALY     — tick timestamp outside valid range
      QUALITY_DEGRADATION   — DataConfidenceScore below threshold
      PROVIDER_FAILURE      — provider returned HTTP 5xx or connection refused
      SCHEMA_VIOLATION      — response failed schema validation
      OI_ANOMALY            — OI value outside plausible range
    """

    __tablename__ = "data_incident"

    incident_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    incident_type: Mapped[str] = mapped_column(String(32), nullable=False)
    instrument_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    provider: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    exchange: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    interval_str: Mapped[Optional[str]] = mapped_column(String(4), nullable=True)

    # ── Temporal scope ────────────────────────────────────────────────────
    detected_at: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("NOW()")
    )
    incident_start: Mapped[Optional[datetime.datetime]] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True,
        comment="Start of the affected data window",
    )
    incident_end: Mapped[Optional[datetime.datetime]] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True,
        comment="End of the affected window (NULL if still open)",
    )
    resolved_at: Mapped[Optional[datetime.datetime]] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )

    # ── Severity and status ───────────────────────────────────────────────
    severity: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="MEDIUM",
        comment="CRITICAL | HIGH | MEDIUM | LOW",
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="OPEN"
    )

    # ── Details ───────────────────────────────────────────────────────────
    description: Mapped[Optional[str]] = mapped_column(Text(), nullable=True)
    affected_rows: Mapped[Optional[int]] = mapped_column(BigInteger(), nullable=True)
    gap_count: Mapped[Optional[int]] = mapped_column(BigInteger(), nullable=True)
    recovery_attempts: Mapped[int] = mapped_column(
        BigInteger(), nullable=False, server_default="0"
    )
    resolution_note: Mapped[Optional[str]] = mapped_column(Text(), nullable=True)
    session_date: Mapped[Optional[datetime.date]] = mapped_column(
        Date(), nullable=True
    )

    __table_args__ = (
        CheckConstraint(_INCIDENT_TYPE_CHECK, name="di_incident_type_valid"),
        CheckConstraint(_INCIDENT_STATUS_CHECK, name="di_status_valid"),
        CheckConstraint(
            "severity IN ('CRITICAL','HIGH','MEDIUM','LOW')",
            name="di_severity_valid",
        ),
        Index(
            "di_instrument_type_detected",
            "instrument_id", "incident_type", text("detected_at DESC"),
        ),
        Index(
            "di_open_incidents", "status", text("detected_at DESC"),
            postgresql_where=text("status = 'OPEN'"),
        ),
        Index("di_provider_detected", "provider", text("detected_at DESC")),
    )

    def __repr__(self) -> str:
        return (
            f"<DataIncident {self.incident_type!r} "
            f"instrument={self.instrument_id!r} status={self.status!r} "
            f"detected={self.detected_at!r}>"
        )

class ClosingAuctionSnapshot(Base):
    """Closing Auction Session (CAS) indicative price snapshot.

    Stores the indicative equilibrium price and related quantities during
    NSE/BSE Closing Auction Session.

    CRITICAL SEMANTIC RULE:
    The ``indicative_equilibrium_price`` field is NOT a traded price.
    It represents the indicative price at which the auction would clear
    if it closed at that moment.  It MUST NOT be stored in or confused
    with any LTP field, candle close field, or traded price field.

    CAS runs approximately 15:40–16:00 IST for CAS-eligible securities.
    The final auction uncrossing price becomes the official closing price
    after the session ends.

    Source: Upstox Closing Auction Session support (August/September 2026).
    """

    __tablename__ = "closing_auction_snapshot"

    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    instrument_id: Mapped[str] = mapped_column(String(64), nullable=False)
    exchange: Mapped[str] = mapped_column(String(8), nullable=False)
    session_date: Mapped[datetime.date] = mapped_column(Date(), nullable=False)
    timestamp: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, primary_key=True,
        comment="Time of this CAS snapshot",
    )
    received_at: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("NOW()")
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)

    # ── CAS indicative price fields — NOT LTP ─────────────────────────────
    indicative_equilibrium_price: Mapped[Optional[float]] = mapped_column(
        Numeric(precision=18, scale=6), nullable=True,
        comment="Indicative auction clearing price — NOT a traded LTP",
    )
    indicative_equilibrium_quantity: Mapped[Optional[int]] = mapped_column(
        BigInteger(), nullable=True,
        comment="Indicative matched quantity at equilibrium price",
    )
    total_indicative_quantity: Mapped[Optional[int]] = mapped_column(
        BigInteger(), nullable=True,
        comment="Total indicative order book quantity",
    )
    market_indicative_imbalance: Mapped[Optional[int]] = mapped_column(
        BigInteger(), nullable=True,
        comment="Excess buy or sell quantity at equilibrium price (signed)",
    )
    reference_price: Mapped[Optional[float]] = mapped_column(
        Numeric(precision=18, scale=6), nullable=True,
        comment="Reference price used for CAS eligibility check",
    )

    # ── Market context ────────────────────────────────────────────────────
    # The prevailing LTP before CAS started (for comparison purposes)
    pre_cas_ltp: Mapped[Optional[float]] = mapped_column(
        Numeric(precision=18, scale=6), nullable=True,
        comment="LTP at time of CAS phase entry — for reference only",
    )
    is_final: Mapped[bool] = mapped_column(
        Boolean(), nullable=False, server_default="FALSE",
        comment="TRUE for the final snapshot before auction uncrossing",
    )
    quality_status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="TRUSTED"
    )

    __table_args__ = (
        CheckConstraint(_QUALITY_STATUS_CHECK, name="cas_quality_status_valid"),
        UniqueConstraint(
            "instrument_id", "session_date", "timestamp", "provider",
            name="cas_instrument_date_ts_provider_uq",
        ),
        Index(
            "cas_instrument_date",
            "instrument_id", "session_date", text("timestamp DESC"),
        ),
        Index("cas_exchange_date", "exchange", "session_date"),
    )

    def __repr__(self) -> str:
        return (
            f"<ClosingAuctionSnapshot {self.instrument_id!r} "
            f"date={self.session_date!r} ts={self.timestamp!r} "
            f"ieq_price={self.indicative_equilibrium_price}>"
        )
