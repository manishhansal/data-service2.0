"""
Canonical Pydantic v2 schema for data provenance and lineage.

Every market data observation is assigned a unique ``dataObservationId``
(UUID v4) at the moment of ingestion and an immutable ``DataProvenance``
record is written alongside it.

The provenance record is:
- Assigned at ingestion time (never re-assigned).
- Read-only after creation; any mutation attempt is rejected with HTTP 405.
- Persisted to ``data_provenance`` table within 1,000ms (Requirement 8.6).
- Retained in an in-memory LRU for the 100,000 most-recent observations
  and indefinitely in the database (Requirement 8.3).

Requirements: 8.1, 8.2
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class DataSource(str, Enum):
    """Identifies the origin of the market data observation.

    Maps to the ``source`` field on :class:`DataProvenance`.
    """

    ANGEL_ONE = "ANGEL_ONE"
    UPSTOX = "UPSTOX"
    SCRAPLING_NSE = "SCRAPLING_NSE"
    JUGAAD_DATA = "JUGAAD_DATA"
    OPENCHART = "OPENCHART"
    YAHOO_FINANCE = "YAHOO_FINANCE"
    BINANCE = "BINANCE"
    DERIBIT = "DERIBIT"
    UNKNOWN = "UNKNOWN"


class DataTrustStatus(str, Enum):
    """Confidence classification assigned during the validation pipeline."""

    TRUSTED = "TRUSTED"
    POOR_QUALITY = "POOR_QUALITY"
    FALLBACK = "FALLBACK"
    DEGRADED = "DEGRADED"


class ProviderSourceType(str, Enum):
    """Authentication / credential classification of the upstream provider."""

    BROKER_AUTHENTICATED = "BROKER_AUTHENTICATED"
    OPEN_SOURCE_NSE_DERIVED = "OPEN_SOURCE_NSE_DERIVED"
    CREDENTIAL_FREE = "CREDENTIAL_FREE"
    SECONDARY_FALLBACK = "SECONDARY_FALLBACK"


# ---------------------------------------------------------------------------
# DataProvenance model
# ---------------------------------------------------------------------------


class DataProvenance(BaseModel):
    """Immutable provenance record for a single market data observation.

    Assigned at ingestion; never mutated after creation.

    Attributes:
        dataObservationId: UUID v4 assigned at ingestion — globally unique.
        source:            Which provider supplied this observation.
        sourceVersion:     Provider API version string (e.g. ``"v2"``).
        eventTimeMs:       Exchange event time in UTC milliseconds.
        receivedAtMs:      Platform ingestion wall-clock time in UTC ms.
        availableAtMs:     When the data was made available to consumers
                           (after validation pipeline completes), UTC ms.
        normalisationVersion: Semver string (e.g. ``"1.0.0"``) identifying
                           which normaliser version processed this record.
                           Allows re-normalisation when schema changes.
        validationApplied: Whether the full 14-step pipeline was applied.
        isFallback:        True if this observation came from a fallback
                           provider rather than the primary one.
        fallbackReason:    Human-readable reason string when isFallback is
                           True; None otherwise.
        sourceChain:       Ordered list of provider IDs in the data's
                           processing path (primary → fallback → …).
        instrumentId:      Canonical instrument identifier (used for lineage
                           lookups by instrument).
        exchange:          Exchange the data belongs to (e.g. ``"NSE"``).
        intervalStr:       Candle interval string (e.g. ``"1m"``); None for
                           tick / live-quote data.
        sessionDate:       IST trading day as ``YYYY-MM-DD``; None for
                           non-session data.
        provider:          String name of the provider (duplicates ``source``
                           as a plain string for DB compatibility).
        sourceType:        Credential / authentication classification.
        authenticated:     Whether the provider required authentication.
        datasetKey:        Opaque string key for the dataset (used to
                           correlate rows in ``data_provenance`` table).
        dataAsOf:          UTC timestamp of the most-recent data point in
                           this observation; None for live ticks.
        responseHash:      SHA-256 hex digest of the raw provider response.
        fromTs:            Start of the requested data window, UTC.
        toTs:              End of the requested data window, UTC.
        datasetVersion:    Monotonically increasing integer; incremented each
                           time a dataset for the same key is refreshed.
        dataTrustStatus:   Pipeline-assigned quality classification.
        rowCount:          Number of candles / records in this observation.
    """

    model_config = ConfigDict(
        frozen=True,           # provenance is immutable after creation
        populate_by_name=True,
    )

    # ── Core identity ────────────────────────────────────────────────────────
    dataObservationId: UUID = Field(
        default_factory=uuid4,
        description="UUID v4 assigned at ingestion — globally unique.",
    )
    source: DataSource = Field(
        default=DataSource.UNKNOWN,
        description="Provider that supplied this observation.",
    )
    sourceVersion: Optional[str] = Field(
        default=None,
        max_length=32,
        description="Provider API version string (e.g. 'v2').",
    )

    # ── Timing ───────────────────────────────────────────────────────────────
    eventTimeMs: Optional[int] = Field(
        default=None,
        ge=0,
        description="Exchange event time in UTC milliseconds.",
    )
    receivedAtMs: Optional[int] = Field(
        default=None,
        ge=0,
        description="Platform ingestion wall-clock time in UTC ms.",
    )
    availableAtMs: Optional[int] = Field(
        default=None,
        ge=0,
        description=(
            "When the record became available to consumers (post-pipeline), UTC ms. "
            "Used for backtest point-in-time correctness (Requirement 23.4)."
        ),
    )

    # ── Normalisation ────────────────────────────────────────────────────────
    normalisationVersion: str = Field(
        default="2.0.0",
        max_length=16,
        description="Semver version of the normaliser that processed this record.",
    )
    validationApplied: bool = Field(
        default=True,
        description="Whether the full 14-step validation pipeline was applied.",
    )

    # ── Fallback tracking ────────────────────────────────────────────────────
    isFallback: bool = Field(
        default=False,
        description="True when data came from a fallback rather than primary provider.",
    )
    fallbackReason: Optional[str] = Field(
        default=None,
        max_length=500,
        description="Reason for fallback; None when isFallback is False.",
    )
    sourceChain: list[str] = Field(
        default_factory=list,
        description=(
            "Ordered list of provider IDs in the processing path. "
            "First entry is the original live provider even when served from cache."
        ),
    )

    # ── Instrument context ───────────────────────────────────────────────────
    instrumentId: Optional[str] = Field(
        default=None,
        max_length=64,
        description="Canonical instrument identifier.",
    )
    exchange: Optional[str] = Field(
        default=None,
        max_length=8,
        description="Exchange code (e.g. 'NSE', 'NFO', 'BINANCE').",
    )
    intervalStr: Optional[str] = Field(
        default=None,
        max_length=4,
        description="Candle interval string (e.g. '1m'); None for tick data.",
    )
    sessionDate: Optional[str] = Field(
        default=None,
        description="IST trading day as YYYY-MM-DD; None for non-session data.",
        pattern=r"^\d{4}-\d{2}-\d{2}$|^$",
    )

    # ── DB / persistence fields ──────────────────────────────────────────────
    provider: Optional[str] = Field(
        default=None,
        max_length=32,
        description="String provider name (mirrors source.value for DB compatibility).",
    )
    sourceType: Optional[ProviderSourceType] = Field(
        default=None,
        description="Credential / authentication classification.",
    )
    authenticated: bool = Field(
        default=False,
        description="Whether the provider call required authentication.",
    )
    datasetKey: Optional[str] = Field(
        default=None,
        max_length=256,
        description="Opaque dataset key correlating rows in data_provenance table.",
    )
    dataAsOf: Optional[datetime] = Field(
        default=None,
        description="UTC datetime of the most-recent data point in this observation.",
    )
    responseHash: Optional[str] = Field(
        default=None,
        max_length=64,
        description="SHA-256 hex digest of the raw provider response.",
    )
    fromTs: Optional[datetime] = Field(
        default=None,
        description="Start of the requested data window, UTC.",
    )
    toTs: Optional[datetime] = Field(
        default=None,
        description="End of the requested data window, UTC.",
    )
    datasetVersion: Optional[int] = Field(
        default=None,
        ge=0,
        description="Monotonically increasing version counter for the dataset key.",
    )
    dataTrustStatus: DataTrustStatus = Field(
        default=DataTrustStatus.TRUSTED,
        description="Pipeline-assigned quality classification.",
    )
    rowCount: Optional[int] = Field(
        default=None,
        ge=0,
        description="Number of candles / records in this observation.",
    )


# ---------------------------------------------------------------------------
# Helpers (internal)
# ---------------------------------------------------------------------------


def _utc_now_iso() -> str:
    """Return the current UTC time as a Z-suffixed ISO-8601 string (ms precision)."""
    from datetime import UTC  # noqa: PLC0415

    now = datetime.now(UTC)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _sha256_hex(raw: bytes | str) -> str:
    """Return the SHA-256 hex digest of *raw*."""
    import hashlib  # noqa: PLC0415

    if isinstance(raw, str):
        raw = raw.encode()
    return hashlib.sha256(raw).hexdigest()


# ---------------------------------------------------------------------------
# ProvenanceFactory
# ---------------------------------------------------------------------------


class ProvenanceFactory:
    """Ergonomic builder for :class:`DataProvenance` records.

    All public methods assign a fresh UUID v4 ``dataObservationId``, set
    ``receivedAtMs`` to the current UTC epoch milliseconds, and populate
    ``availableAtMs`` at construction time so callers never need to
    generate UUIDs or timestamps manually.

    Usage
    -----
    ::

        prov = ProvenanceFactory.create(
            instrument_id="NSE:NIFTY50:IDX",
            exchange="NSE",
            primary_provider=DataSource.ANGEL_ONE,
        )

        prov_from_tick = ProvenanceFactory.from_tick(tick_dict)

    Requirements: 8.1, 8.2
    """

    # Default normalisation pipeline version.
    DEFAULT_NORMALISATION_VERSION: str = "2.0.0"

    @classmethod
    def create(
        cls,
        instrument_id: str,
        exchange: str,
        primary_provider: "DataSource | str",
        *,
        source_version: Optional[str] = None,
        is_fallback: bool = False,
        fallback_reason: Optional[str] = None,
        event_time_ms: Optional[int] = None,
        normalisation_version: str = DEFAULT_NORMALISATION_VERSION,
        interval_str: Optional[str] = None,
        session_date: Optional[str] = None,
        source_type: Optional[ProviderSourceType] = None,
        authenticated: bool = False,
        dataset_key: Optional[str] = None,
        dataset_version: Optional[int] = None,
        row_count: Optional[int] = None,
        source_payload: Optional["bytes | str"] = None,
    ) -> "DataProvenance":
        """Create a new :class:`DataProvenance` for a freshly ingested observation.

        Parameters
        ----------
        instrument_id:
            Platform-canonical instrument identifier (e.g. ``"NSE:NIFTY50:IDX"``).
        exchange:
            Exchange / venue the data originated from (e.g. ``"NSE"``).
        primary_provider:
            :class:`DataSource` enum member or plain string name of the
            upstream provider that served this observation.
        source_version:
            Provider API version string (e.g. ``"v2"``).
        is_fallback:
            Set to ``True`` when the data came from a fallback provider.
        fallback_reason:
            Human-readable reason why the fallback was used.
        event_time_ms:
            Exchange event time in UTC milliseconds; ``None`` for tick data
            without an explicit event timestamp.
        normalisation_version:
            Semver string of the normalisation pipeline.  Defaults to
            ``ProvenanceFactory.DEFAULT_NORMALISATION_VERSION``.
        interval_str:
            Candle interval (e.g. ``"1m"``); ``None`` for live-quote / tick data.
        session_date:
            IST trading day as ``YYYY-MM-DD``; ``None`` for non-session data.
        source_type:
            Authentication / credential classification of the provider.
        authenticated:
            Whether the provider call required authentication.
        dataset_key:
            Opaque key correlating rows in the ``data_provenance`` table.
        dataset_version:
            Monotonically increasing version counter for the dataset key.
        row_count:
            Number of candles / records in this observation.
        source_payload:
            Optional raw provider payload (``bytes`` or ``str``).  When
            supplied its SHA-256 digest is stored in ``responseHash``.

        Returns
        -------
        DataProvenance
            A fully-populated, immutable provenance record with a unique
            ``dataObservationId`` and timestamps set to *now* (UTC).
        """
        from datetime import UTC  # noqa: PLC0415

        now_ms = int(datetime.now(UTC).timestamp() * 1000)

        # Normalise primary_provider to DataSource enum when given as string
        if isinstance(primary_provider, str):
            try:
                source_enum = DataSource(primary_provider.upper())
            except ValueError:
                source_enum = DataSource.UNKNOWN
        else:
            source_enum = primary_provider

        provider_str = (
            primary_provider
            if isinstance(primary_provider, str)
            else primary_provider.value
        )

        response_hash: Optional[str] = (
            _sha256_hex(source_payload) if source_payload is not None else None
        )

        return DataProvenance(
            source=source_enum,
            sourceVersion=source_version,
            eventTimeMs=event_time_ms,
            receivedAtMs=now_ms,
            availableAtMs=now_ms,
            normalisationVersion=normalisation_version,
            validationApplied=True,
            isFallback=is_fallback,
            fallbackReason=fallback_reason,
            sourceChain=[provider_str],
            instrumentId=instrument_id,
            exchange=exchange,
            intervalStr=interval_str,
            sessionDate=session_date,
            provider=provider_str,
            sourceType=source_type,
            authenticated=authenticated,
            datasetKey=dataset_key,
            datasetVersion=dataset_version,
            responseHash=response_hash,
            dataTrustStatus=DataTrustStatus.TRUSTED,
            rowCount=row_count,
        )

    @classmethod
    def from_tick(
        cls,
        tick_dict: "dict",
        *,
        normalisation_version: str = DEFAULT_NORMALISATION_VERSION,
        source_payload: Optional["bytes | str"] = None,
    ) -> "DataProvenance":
        """Create a :class:`DataProvenance` from a tick event dict.

        The tick dict must contain:
        - ``instrumentId`` (str)
        - ``exchange`` (str)
        - ``source`` (str) — name of the originating provider

        Optional tick keys:
        - ``eventTimeMs`` (int) — exchange event timestamp in UTC ms

        Parameters
        ----------
        tick_dict:
            Dict representing a normalised tick event (e.g. from the
            Streaming Engine or an upstream provider WebSocket frame).
        normalisation_version:
            Semver string of the normalisation pipeline.
        source_payload:
            Optional raw provider payload for SHA-256 hashing.

        Returns
        -------
        DataProvenance
            A freshly assigned provenance record.

        Raises
        ------
        KeyError
            If ``tick_dict`` is missing a required key
            (``instrumentId``, ``exchange``, or ``source``).
        """
        instrument_id: str = tick_dict["instrumentId"]
        exchange: str = tick_dict["exchange"]
        primary_provider: str = tick_dict["source"]
        event_time_ms: Optional[int] = tick_dict.get("eventTimeMs")

        return cls.create(
            instrument_id=instrument_id,
            exchange=exchange,
            primary_provider=primary_provider,
            event_time_ms=event_time_ms,
            normalisation_version=normalisation_version,
            source_payload=source_payload,
        )
