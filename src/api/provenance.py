"""
src/api/provenance.py
=====================

Provenance and Lineage REST API endpoints for DATA-SERVICE 2.0.

Endpoints (Task 10.4):

``GET  /v1/provenance/{observation_id}``
    Return the full :class:`DataProvenance` record for a single observation.
    HTTP 404 if the observation ID is not in the lineage store.
    Response is served within 500ms (Requirement 8.4).

``GET  /v1/provenance/instrument/{instrument_id}``
    Return the most-recent N provenance records for an instrument, ordered by
    ``receivedAtMs DESC``.  Query param ``?limit`` (default 100, max 1000).
    Returns ``storeSize`` and ``totalRecorded`` in the metadata envelope
    (Requirement 8.5).

``POST /v1/provenance``
    Create / record a new provenance record (internal service use).
    Body fields map to :meth:`ProvenanceFactory.create` parameters.
    Returns the created record wrapped in the canonical success envelope.

``GET  /v1/lineage/trade/{trade_id}``
    Return a :class:`TradeForensicsRecord` by trade ID.
    HTTP 404 if not found (Requirement 8.8).

``GET  /v1/lineage/strategy/{strategy_id}``
    Return recent forensics records for a strategy.  ``?limit`` default 50,
    max 1000.

``GET  /v1/lineage/instrument/{instrument_id}``
    Return recent forensics records for an instrument.  ``?limit`` default 50,
    max 1000.

All responses use the canonical success envelope::

    {"data": <payload>, "metadata": {...}}

All errors use the canonical error envelope::

    {"error": {"code": <str>, "message": <str>, "requestId": <str>}}

All provenance fields are read-only; PUT/PATCH/DELETE on a provenance record
returns HTTP 405 (Requirement 8.9).

Requirements: 8.4, 8.5, 8.7, 8.8, 8.9
"""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timezone
from typing import Annotated, Any, Optional

import structlog
from fastapi import APIRouter, Query, Request
from fastapi.responses import Response

from src.core.schemas.provenance import DataProvenance, DataSource, ProvenanceFactory

_log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# JSON serialisation helpers
# ---------------------------------------------------------------------------


class _DateAwareEncoder(json.JSONEncoder):
    """Extend stdlib JSONEncoder to handle ``date``, ``datetime``, and UUID."""

    def default(self, o: Any) -> Any:  # type: ignore[override]
        if isinstance(o, datetime):
            return o.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        if isinstance(o, date):
            return o.isoformat()
        try:
            import uuid as _uuid  # noqa: PLC0415

            if isinstance(o, _uuid.UUID):
                return str(o)
        except ImportError:
            pass
        return super().default(o)


def _json_response(content: Any, *, status_code: int = 200) -> Response:
    body = json.dumps(content, cls=_DateAwareEncoder)
    return Response(content=body, status_code=status_code, media_type="application/json")


def _utc_iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _request_id() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Canonical response envelopes
# ---------------------------------------------------------------------------


def _success_envelope(
    data: Any,
    *,
    data_source_type: str = "LIVE",
    data_as_of: Optional[str] = None,
    provider: Optional[str] = None,
    extra_meta: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Wrap *data* in the canonical success envelope (Requirement 16.2)."""
    meta: dict[str, Any] = {
        "requestedAt": _utc_iso_now(),
        "dataAsOf": data_as_of or _utc_iso_now(),
        "dataSourceType": data_source_type,
        "provider": provider,
    }
    if extra_meta:
        meta.update(extra_meta)
    return {"data": data, "metadata": meta}


def _error_envelope(
    code: str,
    message: str,
    *,
    request_id: Optional[str] = None,
    provider: Optional[str] = None,
    retry_after_ms: Optional[int] = None,
) -> dict[str, Any]:
    """Canonical error envelope (Requirement 16.3)."""
    return {
        "error": {
            "code": code,
            "message": message,
            "provider": provider,
            "retryAfterMs": retry_after_ms,
            "requestId": request_id or _request_id(),
        }
    }


# ---------------------------------------------------------------------------
# Store helpers — lazy resolve from app.state
# ---------------------------------------------------------------------------


def _get_lineage_store(request: Request):  # type: ignore[return]
    """Resolve (or lazily create) the :class:`LineageStore` on app state."""
    store = getattr(request.app.state, "lineage_store", None)
    if store is None:
        from src.stores.lineage_store import LineageStore  # noqa: PLC0415

        db_engine = getattr(request.app.state, "db_engine", None)
        store = LineageStore(db_engine=db_engine)
        request.app.state.lineage_store = store
    return store


def _get_forensics_store(request: Request):  # type: ignore[return]
    """Resolve (or lazily create) the :class:`TradeForensicsStore` on app state."""
    store = getattr(request.app.state, "trade_forensics_store", None)
    if store is None:
        from src.forensics.trade_forensics import TradeForensicsStore  # noqa: PLC0415

        store = TradeForensicsStore()
        request.app.state.trade_forensics_store = store
    return store


# ---------------------------------------------------------------------------
# Helpers — serialise model instances to JSON-safe dicts
# ---------------------------------------------------------------------------


def _provenance_to_dict(prov: DataProvenance) -> dict[str, Any]:
    """Convert a :class:`DataProvenance` to a JSON-serialisable dict."""
    raw = prov.model_dump(mode="python")
    # Ensure UUID and datetime fields are serialised to strings.
    result: dict[str, Any] = {}
    for k, v in raw.items():
        if isinstance(v, uuid.UUID):
            result[k] = str(v)
        elif isinstance(v, datetime):
            result[k] = v.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        elif isinstance(v, date):
            result[k] = v.isoformat()
        else:
            result[k] = v
    return result


def _forensics_to_dict(rec: Any) -> dict[str, Any]:
    """Convert a :class:`TradeForensicsRecord` to a JSON-serialisable dict."""
    return rec.model_dump(mode="python")


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter()

# Limit constants
_PROVENANCE_DEFAULT_LIMIT = 100
_PROVENANCE_MAX_LIMIT = 1000
_FORENSICS_DEFAULT_LIMIT = 50
_FORENSICS_MAX_LIMIT = 1000


# ===========================================================================
# Provenance endpoints
# ===========================================================================


# ---------------------------------------------------------------------------
# GET /v1/provenance/{observation_id}
# ---------------------------------------------------------------------------


@router.get(
    "/provenance/{observation_id}",
    summary="Fetch a provenance record by observation ID",
    description=(
        "Returns the full DataProvenance record for a single observation. "
        "HTTP 404 if the observation ID is not found in the lineage store. "
        "Response is served within 500ms. (Requirement 8.4)"
    ),
    response_class=Response,
)
async def get_provenance(
    request: Request,
    observation_id: str,
) -> Response:
    """Return the full provenance record for *observation_id*.

    The lineage store checks its in-memory LRU first, then falls back to
    the database when the record has been evicted from cache.

    Returns HTTP 404 when no record is found in either location.
    """
    req_id = _request_id()
    store = _get_lineage_store(request)

    prov = await store.get(observation_id)
    if prov is None:
        return _json_response(
            _error_envelope(
                "OBSERVATION_NOT_FOUND",
                f"No provenance record found for observationId='{observation_id}'.",
                request_id=req_id,
            ),
            status_code=404,
        )

    return _json_response(
        _success_envelope(
            _provenance_to_dict(prov),
            data_source_type="HISTORICAL",
            provider=prov.provider,
        )
    )


# ---------------------------------------------------------------------------
# GET /v1/provenance/instrument/{instrument_id}
# ---------------------------------------------------------------------------


@router.get(
    "/provenance/instrument/{instrument_id}",
    summary="List recent provenance records for an instrument",
    description=(
        "Returns the most-recent N provenance records for the given instrument, "
        "ordered by receivedAtMs DESC. "
        "N must be 1–1000; defaults to 100. "
        "Includes storeSize and totalRecorded in the metadata. "
        "(Requirement 8.5)"
    ),
    response_class=Response,
)
async def get_provenance_by_instrument(
    request: Request,
    instrument_id: str,
    limit: Annotated[
        int,
        Query(
            description=(
                f"Maximum number of records to return "
                f"(1–{_PROVENANCE_MAX_LIMIT}). Default: {_PROVENANCE_DEFAULT_LIMIT}."
            ),
            ge=1,
            le=_PROVENANCE_MAX_LIMIT,
        ),
    ] = _PROVENANCE_DEFAULT_LIMIT,
) -> Response:
    """Return recent provenance records for *instrument_id*.

    - ``limit``: 1–1000; defaults to 100.
    - Results ordered by ``receivedAtMs`` DESC (newest first).
    - ``metadata.storeSize``: current in-memory record count.
    - ``metadata.totalRecorded``: cumulative count since platform start.
    """
    store = _get_lineage_store(request)

    records = await store.get_by_instrument(instrument_id, limit=limit)
    payload = [_provenance_to_dict(r) for r in records]

    return _json_response(
        _success_envelope(
            payload,
            data_source_type="HISTORICAL",
            extra_meta={
                "storeSize": store.cache_size(),
                "totalRecorded": store.total_recorded,
                "instrument_id": instrument_id,
                "limit": limit,
                "count": len(payload),
            },
        )
    )


# ---------------------------------------------------------------------------
# POST /v1/provenance
# ---------------------------------------------------------------------------


@router.post(
    "/provenance",
    summary="Create a new provenance record (internal)",
    description=(
        "Create and store a new DataProvenance record. "
        "Intended for internal service use when ingesting a new observation. "
        "Returns the created record in the canonical success envelope."
    ),
    response_class=Response,
)
async def create_provenance(
    request: Request,
) -> Response:
    """Create a new provenance record from the JSON body.

    Accepted body fields (all optional except ``instrumentId``, ``exchange``,
    and ``primaryProvider`` / ``source``):

    .. code-block:: json

        {
            "instrumentId":           "NSE:NIFTY50:IDX",
            "exchange":               "NSE",
            "primaryProvider":        "ANGEL_ONE",
            "sourceVersion":          "v2",
            "isFallback":             false,
            "fallbackReason":         null,
            "eventTimeMs":            1700000000000,
            "normalisationVersion":   "2.0.0",
            "intervalStr":            "1m",
            "sessionDate":            "2025-01-15",
            "sourceType":             "BROKER_AUTHENTICATED",
            "authenticated":          true,
            "datasetKey":             "NSE:NIFTY50:1m:2025-01-15",
            "datasetVersion":         1,
            "rowCount":               390
        }

    The ``dataObservationId`` is always assigned by the platform (UUID v4);
    callers must not supply one.
    """
    req_id = _request_id()

    try:
        body: dict[str, Any] = await request.json()
    except Exception:  # noqa: BLE001
        return _json_response(
            _error_envelope(
                "INVALID_BODY",
                "Request body must be valid JSON.",
                request_id=req_id,
            ),
            status_code=400,
        )

    instrument_id: Optional[str] = body.get("instrumentId")
    exchange: Optional[str] = body.get("exchange")
    primary_provider: Optional[str] = body.get("primaryProvider") or body.get("source")

    if not instrument_id:
        return _json_response(
            _error_envelope(
                "MISSING_FIELD",
                "Required field 'instrumentId' is missing.",
                request_id=req_id,
            ),
            status_code=400,
        )
    if not exchange:
        return _json_response(
            _error_envelope(
                "MISSING_FIELD",
                "Required field 'exchange' is missing.",
                request_id=req_id,
            ),
            status_code=400,
        )
    if not primary_provider:
        return _json_response(
            _error_envelope(
                "MISSING_FIELD",
                "Required field 'primaryProvider' (or 'source') is missing.",
                request_id=req_id,
            ),
            status_code=400,
        )

    # Map source_type string → enum (optional).
    from src.core.schemas.provenance import ProviderSourceType  # noqa: PLC0415

    source_type: Optional[ProviderSourceType] = None
    raw_source_type = body.get("sourceType")
    if raw_source_type:
        try:
            source_type = ProviderSourceType(raw_source_type)
        except ValueError:
            return _json_response(
                _error_envelope(
                    "INVALID_FIELD",
                    f"Invalid sourceType '{raw_source_type}'. "
                    f"Valid values: {[e.value for e in ProviderSourceType]}.",
                    request_id=req_id,
                ),
                status_code=400,
            )

    # Build provenance using the factory (assigns UUID + timestamps).
    prov = ProvenanceFactory.create(
        instrument_id=instrument_id,
        exchange=exchange,
        primary_provider=primary_provider,
        source_version=body.get("sourceVersion"),
        is_fallback=bool(body.get("isFallback", False)),
        fallback_reason=body.get("fallbackReason"),
        event_time_ms=body.get("eventTimeMs"),
        normalisation_version=body.get(
            "normalisationVersion",
            ProvenanceFactory.DEFAULT_NORMALISATION_VERSION,
        ),
        interval_str=body.get("intervalStr"),
        session_date=body.get("sessionDate"),
        source_type=source_type,
        authenticated=bool(body.get("authenticated", False)),
        dataset_key=body.get("datasetKey"),
        dataset_version=body.get("datasetVersion"),
        row_count=body.get("rowCount"),
    )

    # Persist to lineage store.
    store = _get_lineage_store(request)
    await store.put(str(prov.dataObservationId), prov)

    _log.info(
        "provenance_created",
        observation_id=str(prov.dataObservationId),
        instrument_id=instrument_id,
        provider=primary_provider,
    )

    return _json_response(
        _success_envelope(
            _provenance_to_dict(prov),
            data_source_type="LIVE",
            provider=primary_provider,
        ),
        status_code=201,
    )


# ---------------------------------------------------------------------------
# HTTP 405 catch-all for mutating provenance records
# ---------------------------------------------------------------------------
# Provenance records are immutable after creation (Requirement 8.9).
# Any PUT, PATCH, or DELETE on a provenance record must return HTTP 405.


@router.put(
    "/provenance/{observation_id}",
    include_in_schema=False,
    response_class=Response,
)
@router.patch(
    "/provenance/{observation_id}",
    include_in_schema=False,
    response_class=Response,
)
@router.delete(
    "/provenance/{observation_id}",
    include_in_schema=False,
    response_class=Response,
)
async def provenance_mutation_rejected(
    request: Request,
    observation_id: str,
) -> Response:
    """Reject any mutation attempt on a provenance record (Requirement 8.9)."""
    req_id = _request_id()
    return _json_response(
        _error_envelope(
            "PROVENANCE_IMMUTABLE",
            "Provenance records are read-only and cannot be modified.",
            request_id=req_id,
        ),
        status_code=405,
    )


# ===========================================================================
# Lineage / Trade Forensics endpoints
# ===========================================================================


# ---------------------------------------------------------------------------
# GET /v1/lineage/trade/{trade_id}
# ---------------------------------------------------------------------------


@router.get(
    "/lineage/trade/{trade_id}",
    summary="Fetch a trade forensics record by trade ID",
    description=(
        "Returns the TradeForensicsRecord for a single trade. "
        "HTTP 404 if the trade ID is not found. "
        "(Requirement 8.8)"
    ),
    response_class=Response,
)
async def get_trade_forensics(
    request: Request,
    trade_id: str,
) -> Response:
    """Return the forensics record for *trade_id*.

    The record contains the data observations, quality-gate snapshot, and
    execution timestamp that were in effect when the trade was executed.
    """
    req_id = _request_id()
    store = _get_forensics_store(request)

    rec = store.get(trade_id)
    if rec is None:
        return _json_response(
            _error_envelope(
                "TRADE_NOT_FOUND",
                f"No forensics record found for tradeId='{trade_id}'.",
                request_id=req_id,
            ),
            status_code=404,
        )

    return _json_response(
        _success_envelope(
            _forensics_to_dict(rec),
            data_source_type="HISTORICAL",
        )
    )


# ---------------------------------------------------------------------------
# GET /v1/lineage/strategy/{strategy_id}
# ---------------------------------------------------------------------------


@router.get(
    "/lineage/strategy/{strategy_id}",
    summary="List recent forensics records for a strategy",
    description=(
        "Returns the most-recent forensics records for the given strategy ID, "
        "ordered by executedAt DESC. "
        "?limit defaults to 50, max 1000."
    ),
    response_class=Response,
)
async def get_forensics_by_strategy(
    request: Request,
    strategy_id: str,
    limit: Annotated[
        int,
        Query(
            description=(
                f"Maximum number of records to return "
                f"(1–{_FORENSICS_MAX_LIMIT}). Default: {_FORENSICS_DEFAULT_LIMIT}."
            ),
            ge=1,
            le=_FORENSICS_MAX_LIMIT,
        ),
    ] = _FORENSICS_DEFAULT_LIMIT,
) -> Response:
    """Return recent forensics records for *strategy_id*."""
    store = _get_forensics_store(request)

    records = store.get_by_strategy(strategy_id, limit=limit)
    payload = [_forensics_to_dict(r) for r in records]

    return _json_response(
        _success_envelope(
            payload,
            data_source_type="HISTORICAL",
            extra_meta={
                "strategy_id": strategy_id,
                "limit": limit,
                "count": len(payload),
            },
        )
    )


# ---------------------------------------------------------------------------
# GET /v1/lineage/instrument/{instrument_id}
# ---------------------------------------------------------------------------


@router.get(
    "/lineage/instrument/{instrument_id}",
    summary="List recent forensics records for an instrument",
    description=(
        "Returns the most-recent forensics records for the given instrument ID, "
        "ordered by executedAt DESC. "
        "?limit defaults to 50, max 1000."
    ),
    response_class=Response,
)
async def get_forensics_by_instrument(
    request: Request,
    instrument_id: str,
    limit: Annotated[
        int,
        Query(
            description=(
                f"Maximum number of records to return "
                f"(1–{_FORENSICS_MAX_LIMIT}). Default: {_FORENSICS_DEFAULT_LIMIT}."
            ),
            ge=1,
            le=_FORENSICS_MAX_LIMIT,
        ),
    ] = _FORENSICS_DEFAULT_LIMIT,
) -> Response:
    """Return recent forensics records for *instrument_id*."""
    store = _get_forensics_store(request)

    records = store.get_by_instrument(instrument_id, limit=limit)
    payload = [_forensics_to_dict(r) for r in records]

    return _json_response(
        _success_envelope(
            payload,
            data_source_type="HISTORICAL",
            extra_meta={
                "instrument_id": instrument_id,
                "limit": limit,
                "count": len(payload),
            },
        )
    )
