"""
Instrument API endpoints for DATA-SERVICE 2.0.

Task 3.4 — Requirements 2.5, 2.6, 2.8, 2.9, 11.5

Endpoints:

``GET /v1/instruments``
    List instruments with optional filters. Returns canonical Instrument
    records with provider tokens stripped by default (Requirement 2.7).

``GET /v1/instruments/{instrumentId}``
    Single instrument lookup. Returns provider tokens only when
    ``?include=providerTokens`` is present (Requirement 2.8).

``GET /v1/instruments/fno-universe``
    Current F&O universe snapshot. HTTP 503 when no snapshot is loaded
    (Requirement 2.9, 11.5).

``GET /v1/instruments/fno-universe/history``
    Past F&O universe snapshots, paginated (Requirement 11.5).

All responses use the canonical success envelope:
    {"data": <payload>, "metadata": {...}}

All error responses use the canonical error envelope:
    {"error": {"code": <str>, "message": <str>, "requestId": <str>}}
"""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timezone
from typing import Annotated, Any, Optional

import structlog
from fastapi import APIRouter, Query, Request
from fastapi.responses import Response

from src.core.schemas.instrument import (
    ExchangeEnum,
    FnoUniverseSnapshot,
    InstrumentType,
    SegmentEnum,
)


# ---------------------------------------------------------------------------
# JSON serialisation helpers
#
# ``starlette.responses.JSONResponse`` uses the stdlib ``json`` module, which
# cannot serialise ``datetime.date`` objects.  We use a thin wrapper that
# applies a custom default encoder for date/datetime types that appear in
# Pydantic-dumped dicts.
# ---------------------------------------------------------------------------


class _DateAwareEncoder(json.JSONEncoder):
    """Extend stdlib JSONEncoder to serialise ``date`` and ``datetime``
    objects as ISO-8601 strings.

    - ``date``     → ``"YYYY-MM-DD"``
    - ``datetime`` → ``"YYYY-MM-DDTHH:MM:SS.mmmZ"``
    """

    def default(self, o: Any) -> Any:
        if isinstance(o, datetime):
            return o.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        if isinstance(o, date):
            return o.isoformat()
        return super().default(o)


def _json_response(content: Any, *, status_code: int = 200) -> Response:
    """Return an HTTP ``Response`` with JSON body using the date-aware encoder.

    Args:
        content: Any JSON-serialisable value (dict, list, etc.).
        status_code: HTTP status code (default 200).

    Returns:
        ``fastapi.responses.Response`` with ``application/json`` content-type.
    """
    body = json.dumps(content, cls=_DateAwareEncoder)
    return Response(
        content=body,
        status_code=status_code,
        media_type="application/json",
    )


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter()
_log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Envelope builders
# ---------------------------------------------------------------------------


def _utc_iso_now() -> str:
    """Return the current UTC time as an ISO-8601 string with ``Z`` suffix."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _request_id() -> str:
    """Generate a unique request ID (UUID v4)."""
    return str(uuid.uuid4())


def _success_envelope(
    data: Any,
    *,
    data_as_of: Optional[str] = None,
    provider: Optional[str] = None,
    data_source_type: str = "HISTORICAL",
) -> dict[str, Any]:
    """Wrap *data* in the canonical success response envelope.

    Returns:
        ``{"data": ..., "metadata": {...}}``.
    """
    return {
        "data": data,
        "metadata": {
            "requestedAt": _utc_iso_now(),
            "dataAsOf": data_as_of or _utc_iso_now(),
            "dataSourceType": data_source_type,
            "provider": provider,
        },
    }


def _error_envelope(
    code: str,
    message: str,
    *,
    request_id: Optional[str] = None,
    provider: Optional[str] = None,
    retry_after_ms: Optional[int] = None,
) -> dict[str, Any]:
    """Build the canonical error response envelope.

    Returns:
        ``{"error": {"code": ..., "message": ..., "requestId": ...}}``.
    """
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
# Serialisation helpers
# ---------------------------------------------------------------------------


def _instrument_to_dict(
    instrument: Any, *, include_provider_tokens: bool = False
) -> dict[str, Any]:
    """Serialise an ``Instrument`` to a plain dict for the API response.

    Provider token fields are excluded by default (Requirement 2.7).
    When *include_provider_tokens* is ``True``, token fields are merged in
    using ``Instrument.with_provider_tokens()`` (Requirement 2.8).
    """
    if include_provider_tokens:
        return instrument.with_provider_tokens()
    # model_dump(mode="json") converts dates → ISO-8601 strings automatically.
    return instrument.model_dump(mode="json")


def _snapshot_to_dict(snapshot: FnoUniverseSnapshot) -> dict[str, Any]:
    """Serialise an ``FnoUniverseSnapshot`` to a plain dict.

    Maps ``snapshotVersion`` → ``universeVersion`` per the API contract
    (Requirement 2.6).
    """
    # mode="json" ensures date fields are serialised to strings.
    base = snapshot.model_dump(mode="json")
    base["universeVersion"] = base.pop("snapshotVersion")
    return base


# ---------------------------------------------------------------------------
# GET /v1/instruments
# ---------------------------------------------------------------------------


@router.get(
    "/instruments",
    summary="List instruments",
    description=(
        "List instruments with optional filters. "
        "Returns canonical Instrument records with provider tokens excluded. "
        "Returns an empty list when no instruments match (Requirements 2.5, 2.7)."
    ),
    response_class=Response,
)
async def list_instruments(
    request: Request,
    exchange: Annotated[
        Optional[str],
        Query(description="Exchange filter (NSE, NFO, BSE, BFO, MCX)."),
    ] = None,
    instrumentType: Annotated[
        Optional[str],
        Query(
            description=(
                "Instrument type filter "
                "(EQ, FUTIDX, FUTSTK, OPTIDX, OPTSTK, ETF, IDX)."
            )
        ),
    ] = None,
    underlying: Annotated[
        Optional[str],
        Query(description="Underlying symbol for derivatives (e.g. NIFTY)."),
    ] = None,
    segment: Annotated[
        Optional[str],
        Query(description="Market segment filter (EQ, FO, CD, COM, CDS)."),
    ] = None,
    expiry: Annotated[
        Optional[str],
        Query(description="Expiry date filter as ISO-8601 date (YYYY-MM-DD)."),
    ] = None,
) -> Response:
    """List instruments with optional filters.

    All filters are individually optional.  Passing no filters returns the
    full active universe.  HTTP 200 with an empty list when no instruments
    match.  HTTP 400 for invalid filter values.
    """
    req_id = _request_id()

    # ── Validate enum filters ─────────────────────────────────────────────
    parsed_exchange: Optional[ExchangeEnum] = None
    if exchange is not None:
        try:
            parsed_exchange = ExchangeEnum(exchange.upper())
        except ValueError:
            valid = [e.value for e in ExchangeEnum]
            return _json_response(
                _error_envelope(
                    "INVALID_FILTER",
                    f"Invalid exchange '{exchange}'. Valid values: {valid}",
                    request_id=req_id,
                ),
                status_code=400,
            )

    parsed_instrument_type: Optional[InstrumentType] = None
    if instrumentType is not None:
        try:
            parsed_instrument_type = InstrumentType(instrumentType.upper())
        except ValueError:
            valid_types = [t.value for t in InstrumentType]
            return _json_response(
                _error_envelope(
                    "INVALID_FILTER",
                    f"Invalid instrumentType '{instrumentType}'. Valid values: {valid_types}",
                    request_id=req_id,
                ),
                status_code=400,
            )

    parsed_segment: Optional[SegmentEnum] = None
    if segment is not None:
        try:
            parsed_segment = SegmentEnum(segment.upper())
        except ValueError:
            valid_segs = [s.value for s in SegmentEnum]
            return _json_response(
                _error_envelope(
                    "INVALID_FILTER",
                    f"Invalid segment '{segment}'. Valid values: {valid_segs}",
                    request_id=req_id,
                ),
                status_code=400,
            )

    parsed_expiry: Optional[date] = None
    if expiry is not None:
        try:
            parsed_expiry = date.fromisoformat(expiry)
        except ValueError:
            return _json_response(
                _error_envelope(
                    "INVALID_FILTER",
                    f"Invalid expiry date '{expiry}'. Expected ISO-8601 format YYYY-MM-DD.",
                    request_id=req_id,
                ),
                status_code=400,
            )

    # ── Resolve InstrumentMasterService from app state ────────────────────
    service = getattr(request.app.state, "instrument_master", None)
    if service is None:
        await _log.awarning(
            "instrument_master_unavailable",
            component="instruments_api",
            path="/v1/instruments",
        )
        return _json_response(_success_envelope([]))

    # ── Search ────────────────────────────────────────────────────────────
    instruments = service.search_instruments(
        exchange=parsed_exchange,
        instrument_type=parsed_instrument_type,
        underlying=underlying,
        segment=parsed_segment,
        expiry=parsed_expiry,
        active_only=True,
    )

    payload = [_instrument_to_dict(inst, include_provider_tokens=False) for inst in instruments]
    return _json_response(_success_envelope(payload))


# ---------------------------------------------------------------------------
# IMPORTANT: The two static sub-paths /fno-universe and /fno-universe/history
# MUST be registered BEFORE the dynamic path /{instrumentId} so that FastAPI
# matches them first and does not treat "fno-universe" as an instrumentId.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# GET /v1/instruments/fno-universe
# ---------------------------------------------------------------------------


@router.get(
    "/instruments/fno-universe",
    summary="Current F&O universe snapshot",
    description=(
        "Returns the current NSE F&O eligible universe snapshot. "
        "HTTP 503 when no snapshot has been loaded (Requirements 2.6, 2.9, 11.5)."
    ),
    response_class=Response,
)
async def get_fno_universe(request: Request) -> Response:
    """Return the current active F&O universe snapshot.

    Read path (Requirement 2.6, 2.9, 11.5):
        1. In-memory cache fast path (``FnoUniverseService.get_cached_snapshot``)
           — avoids a DB round-trip on every request once warmed.
        2. **DB fallback** — when the cache is cold (e.g. immediately after an
           API process restart, before the 08:45 IST refresh has run), read
           the authoritative ACTIVE snapshot from PostgreSQL and warm the
           cache.  This closes the defect where a valid persisted snapshot was
           masked by an empty in-memory cache, causing a spurious HTTP 503.

    Explicit failure semantics (Requirement 2.9; ml-service phase §9):
        - ``DATABASE_UNAVAILABLE`` (503) — the DB engine is not wired, so the
          snapshot cannot be authoritatively confirmed.  This is distinct from
          a genuinely empty universe.
        - ``NO_UNIVERSE`` (503) — the DB is reachable but no snapshot has ever
          been written.  An empty universe and an unavailable universe are
          different states and must never both be reported as ``[]``.

    Returns:
        - HTTP 200 with the snapshot + constituent symbols/metadata in the
          canonical success envelope.
        - HTTP 503 with an explicit failure code otherwise.
    """
    from src.engines.fno_universe import FnoUniverseService  # noqa: PLC0415

    req_id = _request_id()

    # ── 1. In-memory fast path ────────────────────────────────────────────
    snapshot = FnoUniverseService.get_cached_snapshot()
    source = "cache"

    # ── 2. DB fallback when the cache is cold ─────────────────────────────
    engine = getattr(request.app.state, "db_engine", None)
    if snapshot is None:
        if engine is None:
            # We cannot distinguish "empty" from "not-yet-loaded" without the
            # DB, so report the honest availability state rather than a
            # misleading empty universe.
            await _log.awarning(
                "fno_universe_db_engine_missing",
                component="instruments_api",
                path="/v1/instruments/fno-universe",
            )
            return _json_response(
                _error_envelope(
                    "DATABASE_UNAVAILABLE",
                    "F&O universe snapshot cannot be served: the database "
                    "engine is not available, so the snapshot state cannot be "
                    "authoritatively determined.",
                    request_id=req_id,
                ),
                status_code=503,
            )

        try:
            snapshot = await FnoUniverseService.get_current_snapshot(engine)
        except Exception as exc:  # noqa: BLE001
            await _log.aerror(
                "fno_universe_db_read_failed",
                component="instruments_api",
                error=str(exc),
            )
            return _json_response(
                _error_envelope(
                    "DATABASE_UNAVAILABLE",
                    "F&O universe snapshot cannot be served: the database "
                    "read failed.",
                    request_id=req_id,
                ),
                status_code=503,
            )

        if snapshot is None:
            # DB reachable, but the universe has genuinely never been built.
            return _json_response(
                _error_envelope(
                    "NO_UNIVERSE",
                    "No F&O universe snapshot has ever been written to the "
                    "database. The snapshot is refreshed at 08:45 IST on every "
                    "trading day.",
                    request_id=req_id,
                ),
                status_code=503,
            )

        # Warm the in-memory cache so subsequent requests hit the fast path.
        FnoUniverseService.warm_cache(snapshot)
        source = "database"

    # ── 3. Attach constituent symbols + metadata (Requirement §8) ─────────
    constituents: list[dict[str, Any]] = []
    coverage = "SNAPSHOT_ONLY"
    if engine is not None:
        try:
            constituents = await _load_fno_constituents(
                engine, snapshot.snapshotVersion
            )
            coverage = "FULL" if constituents else "SNAPSHOT_ONLY"
        except Exception as exc:  # noqa: BLE001
            # Non-fatal: still serve the snapshot header, flag partial coverage.
            await _log.awarning(
                "fno_universe_constituents_load_failed",
                component="instruments_api",
                error=str(exc),
            )
            coverage = "PARTIAL"

    payload = _snapshot_to_dict(snapshot)
    payload["symbols"] = [c["symbol"] for c in constituents]
    payload["instrumentMetadata"] = constituents
    payload["coverage"] = coverage
    payload["source"] = source
    # ``status`` (from _snapshot_to_dict) is the snapshot *lifecycle* status
    # (ACTIVE / SUPERSEDED) and must be preserved.  The ml-service
    # failure-semantics *availability* state (§9) is exposed under a distinct
    # ``availability`` key so the two concerns never collide.
    payload["availability"] = (
        "PARTIAL_UNIVERSE" if coverage == "PARTIAL" else "VALID_UNIVERSE"
    )

    return _json_response(
        _success_envelope(
            payload,
            data_source_type="HISTORICAL",
        )
    )


async def _load_fno_constituents(
    engine: Any, snapshot_version: int
) -> list[dict[str, Any]]:
    """Load the F&O universe constituent symbols + metadata for a snapshot.

    Joins ``fno_universe_membership`` (point-in-time membership rows) with
    ``instrument_master`` to enrich each symbol with the metadata required by
    the ml-service universe-construction contract (§8): exchange, instrument
    type, lot/tick size, validity window, and F&O status.

    Synthetic test rows (``instrument_id LIKE '%TEST%'``) are excluded so that
    the served universe reflects only real tradeable instruments.

    Args:
        engine: AsyncEngine for DB access.
        snapshot_version: Snapshot version whose membership set to resolve.

    Returns:
        List of constituent metadata dicts, ordered by symbol.
    """
    from sqlalchemy import text  # noqa: PLC0415

    sql = text(
        """
        SELECT m.instrument_id,
               m.underlying,
               m.segment,
               m.effective_from,
               m.effective_to,
               m.status,
               im.exchange,
               im.instrument_type,
               im.lot_size,
               im.tick_size,
               im.trading_symbol
          FROM fno_universe_membership m
          JOIN fno_universe_snapshot s
            ON s.id = m.snapshot_id
          LEFT JOIN instrument_master im
            ON im.instrument_id = m.instrument_id
         WHERE s.snapshot_version = :version
           AND m.status = 'ACTIVE'
           AND m.instrument_id NOT LIKE '%TEST%'
         ORDER BY m.instrument_id ASC
        """
    ).bindparams(version=snapshot_version)

    out: list[dict[str, Any]] = []
    async with engine.connect() as conn:
        result = await conn.execute(sql)
        for row in result.mappings():
            out.append(
                {
                    "symbol": row["underlying"] or row["instrument_id"],
                    "instrumentId": row["instrument_id"],
                    "exchange": row["exchange"],
                    "instrumentType": row["instrument_type"],
                    "fnoStatus": row["status"],
                    "validFrom": (
                        str(row["effective_from"]) if row["effective_from"] else None
                    ),
                    "validTo": (
                        str(row["effective_to"]) if row["effective_to"] else None
                    ),
                    "lotSize": row["lot_size"],
                    "tickSize": (
                        float(row["tick_size"]) if row["tick_size"] is not None else None
                    ),
                }
            )
    return out


# ---------------------------------------------------------------------------
# GET /v1/instruments/fno-universe/history
# ---------------------------------------------------------------------------


@router.get(
    "/instruments/fno-universe/history",
    summary="F&O universe snapshot history",
    description=(
        "Returns past F&O universe snapshots, paginated (max 100 per page). "
        "Supports optional filters for ``status`` and ``version`` (Requirement 11.5)."
    ),
    response_class=Response,
)
async def get_fno_universe_history(
    request: Request,
    status: Annotated[
        Optional[str],
        Query(description="Filter by snapshot status (ACTIVE, SUPERSEDED)."),
    ] = None,
    version: Annotated[
        Optional[int],
        Query(description="Filter by exact snapshot version number.", ge=1),
    ] = None,
    page: Annotated[
        int,
        Query(description="Page number (1-indexed).", ge=1),
    ] = 1,
    limit: Annotated[
        int,
        Query(description="Records per page (max 100).", ge=1, le=100),
    ] = 20,
) -> Response:
    """Return paginated history of F&O universe snapshots.

    HTTP 400 when *status* is not one of ``ACTIVE`` or ``SUPERSEDED``.
    Returns an empty list when no snapshots match.
    """
    req_id = _request_id()

    # Validate status filter
    valid_statuses = {"ACTIVE", "SUPERSEDED"}
    if status is not None and status.upper() not in valid_statuses:
        return _json_response(
            _error_envelope(
                "INVALID_FILTER",
                f"Invalid status '{status}'. Valid values: {sorted(valid_statuses)}",
                request_id=req_id,
            ),
            status_code=400,
        )
    normalised_status = status.upper() if status else None

    engine = getattr(request.app.state, "db_engine", None)

    snapshots: list[dict[str, Any]] = []
    total_count: int = 0

    if engine is not None:
        try:
            from sqlalchemy import text  # noqa: PLC0415

            conditions: list[str] = []
            params: dict[str, Any] = {}

            if normalised_status is not None:
                conditions.append("status = :status")
                params["status"] = normalised_status

            if version is not None:
                conditions.append("snapshot_version = :version")
                params["version"] = version

            where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
            offset = (page - 1) * limit

            count_sql = text(
                f"SELECT COUNT(*) FROM fno_universe_snapshot {where_clause}"  # noqa: S608
            ).bindparams(**params)
            data_sql = text(
                f"""
                SELECT snapshot_version, checksum, generated_at,
                       effective_from, effective_to,
                       fno_equity_count, fno_index_count,
                       constituent_count, status
                  FROM fno_universe_snapshot
                  {where_clause}
                 ORDER BY snapshot_version DESC
                 LIMIT :limit OFFSET :offset
                """  # noqa: S608
            ).bindparams(**params, limit=limit, offset=offset)

            async with engine.connect() as conn:
                count_result = await conn.execute(count_sql)
                total_count = count_result.scalar() or 0

                rows_result = await conn.execute(data_sql)
                for row in rows_result.mappings():
                    snap_dict: dict[str, Any] = {
                        "universeVersion": row["snapshot_version"],
                        "checksum": row["checksum"],
                        "generatedAt": str(row["generated_at"]),
                        "effectiveFrom": str(row["effective_from"]),
                        "effectiveTo": (
                            str(row["effective_to"]) if row.get("effective_to") else None
                        ),
                        "fnoEquityCount": row["fno_equity_count"],
                        "fnoIndexCount": row["fno_index_count"],
                        "constituentCount": row["constituent_count"],
                        "status": row["status"],
                    }
                    snapshots.append(snap_dict)

        except Exception as exc:  # noqa: BLE001
            await _log.awarning(
                "fno_universe_history_db_error",
                component="instruments_api",
                error=str(exc),
            )
            # Degrade gracefully: return empty list rather than 5xx

    total_pages = max(1, (total_count + limit - 1) // limit) if total_count > 0 else 1

    return _json_response(
        _success_envelope(
            {
                "snapshots": snapshots,
                "pagination": {
                    "page": page,
                    "limit": limit,
                    "total": total_count,
                    "totalPages": total_pages,
                },
            }
        )
    )


# ---------------------------------------------------------------------------
# GET /v1/instruments/{instrumentId}
# ---------------------------------------------------------------------------


@router.get(
    "/instruments/{instrument_id:path}",
    summary="Single instrument lookup",
    description=(
        "Fetch a single instrument by its canonical instrumentId. "
        "Provider tokens are included only when ``?include=providerTokens`` is "
        "present (Requirements 2.7, 2.8). HTTP 404 when not found."
    ),
    response_class=Response,
)
async def get_instrument(
    request: Request,
    instrument_id: str,
    include: Annotated[
        Optional[str],
        Query(
            alias="include",
            description=(
                "Pass ``providerTokens`` to include provider-specific token fields."
            ),
        ),
    ] = None,
) -> Response:
    """Fetch a single instrument by canonical ``instrumentId``.

    - ``include=providerTokens`` → token fields **included** (Requirement 2.8).
    - Any other value / absent → tokens **excluded** (default, Requirement 2.7).

    Returns:
        - HTTP 200 with the instrument in the canonical success envelope.
        - HTTP 404 when no instrument matches *instrument_id*.
        - HTTP 503 when the InstrumentMaster service is not available.
    """
    req_id = _request_id()

    service = getattr(request.app.state, "instrument_master", None)
    if service is None:
        await _log.awarning(
            "instrument_master_unavailable",
            component="instruments_api",
            path=f"/v1/instruments/{instrument_id}",
        )
        return _json_response(
            _error_envelope(
                "SERVICE_UNAVAILABLE",
                "Instrument master service is not available.",
                request_id=req_id,
            ),
            status_code=503,
        )

    instrument = service.get_instrument(instrument_id)
    if instrument is None:
        return _json_response(
            _error_envelope(
                "NOT_FOUND",
                f"Instrument '{instrument_id}' not found.",
                request_id=req_id,
            ),
            status_code=404,
        )

    include_tokens = (
        include is not None and include.strip().lower() == "providertokens"
    )
    payload = _instrument_to_dict(instrument, include_provider_tokens=include_tokens)

    return _json_response(_success_envelope(payload))
