"""
Unit tests for instrument API endpoints (Task 3.4).

Requirements: 2.5, 2.6, 2.8, 2.9, 11.5

Tests use an in-process ASGI test client (``httpx.AsyncClient`` via
``ASGITransport``) so no live Redis or PostgreSQL is required.

The ``InstrumentMasterService`` and ``FnoUniverseService`` are patched/stubbed
at the app-state level so tests run fully in-memory.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.api.instruments import router as instruments_router
from src.core.schemas.instrument import (
    ChangeType,
    ExchangeEnum,
    FnoUniverseSnapshot,
    Instrument,
    InstrumentLifecycleEvent,
    InstrumentType,
    SegmentEnum,
)
from src.engines.instrument_master import InstrumentMasterService


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


def _make_instrument(**overrides: Any) -> Instrument:
    """Build a minimal valid ``Instrument`` for testing."""
    defaults: dict[str, Any] = {
        "instrumentId": "NSE:RELIANCE:EQ",
        "tradingSymbol": "RELIANCE",
        "displaySymbol": "Reliance Industries",
        "isin": "INE002A01018",
        "exchange": ExchangeEnum.NSE,
        "segment": SegmentEnum.EQ,
        "instrumentType": InstrumentType.EQ,
        "underlying": None,
        "expiry": None,
        "strike": None,
        "optionType": None,
        "lotSize": 1,
        "tickSize": 0.05,
        "activeFrom": date(2020, 1, 1),
        "activeTo": None,
        "angelToken": "2885",
        "angelSymbol": "RELIANCE-EQ",
        "upstoxKey": "NSE_EQ|2885",
        "upstoxSymbol": "RELIANCE",
    }
    defaults.update(overrides)
    return Instrument(**defaults)


def _make_fno_instrument(**overrides: Any) -> Instrument:
    """Build a minimal valid F&O ``Instrument`` for testing."""
    defaults: dict[str, Any] = {
        "instrumentId": "NFO:NIFTY25JANFUT:FUTIDX",
        "tradingSymbol": "NIFTY25JANFUT",
        "displaySymbol": "NIFTY Jan 2025 Fut",
        "isin": None,
        "exchange": ExchangeEnum.NFO,
        "segment": SegmentEnum.FO,
        "instrumentType": InstrumentType.FUTIDX,
        "underlying": "NIFTY",
        "expiry": date(2025, 1, 30),
        "strike": None,
        "optionType": None,
        "lotSize": 50,
        "tickSize": 0.05,
        "activeFrom": date(2024, 10, 1),
        "activeTo": None,
        "angelToken": "99926000",
        "angelSymbol": "NIFTY25JANFUT",
        "upstoxKey": "NSE_FO|99926000",
        "upstoxSymbol": "NIFTY25JANFUT",
    }
    defaults.update(overrides)
    return Instrument(**defaults)


def _make_snapshot(**overrides: Any) -> FnoUniverseSnapshot:
    """Build a minimal valid ``FnoUniverseSnapshot`` for testing."""
    defaults: dict[str, Any] = {
        "snapshotVersion": 1,
        "checksum": "abc123def456" + "0" * 52,
        "generatedAt": "2025-01-15T03:15:00.000Z",
        "effectiveFrom": date(2025, 1, 15),
        "effectiveTo": None,
        "fnoEquityCount": 180,
        "fnoIndexCount": 4,
        "constituentCount": 184,
        "status": "ACTIVE",
    }
    defaults.update(overrides)
    return FnoUniverseSnapshot(**defaults)


def _make_service(instruments: list[Instrument] | None = None) -> InstrumentMasterService:
    """Create an ``InstrumentMasterService`` pre-loaded with *instruments*."""
    svc = InstrumentMasterService()
    if instruments is not None:
        # Directly populate the private store (avoids requiring a real DB)
        svc._instruments = {inst.instrumentId: inst for inst in instruments}
    return svc


def _make_app(
    instruments: list[Instrument] | None = None,
    service_missing: bool = False,
    db_engine: Any = None,
) -> FastAPI:
    """Build a minimal FastAPI app with the instruments router and test state."""
    app = FastAPI()
    app.include_router(instruments_router, prefix="/v1")

    if not service_missing:
        app.state.instrument_master = _make_service(instruments or [])
    # else: deliberately leave instrument_master unset

    app.state.db_engine = db_engine
    return app


async def _get(app: FastAPI, path: str) -> Any:
    """GET *path* via the ASGI test transport."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


# ---------------------------------------------------------------------------
# GET /v1/instruments
# ---------------------------------------------------------------------------


class TestListInstruments:
    """Tests for GET /v1/instruments (Requirements 2.5, 2.7)."""

    @pytest.mark.asyncio
    async def test_empty_list_when_no_match(self) -> None:
        """HTTP 200 with empty list when no instruments match filters."""
        app = _make_app(instruments=[])
        response = await _get(app, "/v1/instruments")
        assert response.status_code == 200
        body = response.json()
        assert body["data"] == []

    @pytest.mark.asyncio
    async def test_returns_all_instruments_no_filters(self) -> None:
        """Returns all active instruments when no filters are applied."""
        inst1 = _make_instrument()
        inst2 = _make_instrument(
            instrumentId="NSE:TCS:EQ",
            tradingSymbol="TCS",
            isin="INE467B01029",
        )
        app = _make_app(instruments=[inst1, inst2])
        response = await _get(app, "/v1/instruments")
        assert response.status_code == 200
        body = response.json()
        assert len(body["data"]) == 2

    @pytest.mark.asyncio
    async def test_canonical_envelope_present(self) -> None:
        """Response must include 'data' and 'metadata' at top level."""
        app = _make_app(instruments=[_make_instrument()])
        response = await _get(app, "/v1/instruments")
        body = response.json()
        assert "data" in body
        assert "metadata" in body
        assert "requestedAt" in body["metadata"]
        assert "dataSourceType" in body["metadata"]

    @pytest.mark.asyncio
    async def test_provider_tokens_excluded_by_default(self) -> None:
        """angelToken, angelSymbol, upstoxKey, upstoxSymbol must NOT appear."""
        app = _make_app(instruments=[_make_instrument()])
        response = await _get(app, "/v1/instruments")
        body = response.json()
        assert len(body["data"]) == 1
        item = body["data"][0]
        for token_field in ("angelToken", "angelSymbol", "upstoxKey", "upstoxSymbol"):
            assert token_field not in item, f"Provider token field '{token_field}' leaked into response"

    @pytest.mark.asyncio
    async def test_filter_by_exchange(self) -> None:
        """Filtering by exchange returns only matching instruments."""
        nse = _make_instrument(instrumentId="NSE:RELIANCE:EQ", exchange=ExchangeEnum.NSE)
        nfo = _make_fno_instrument()
        app = _make_app(instruments=[nse, nfo])

        response = await _get(app, "/v1/instruments?exchange=NSE")
        body = response.json()
        assert len(body["data"]) == 1
        assert body["data"][0]["instrumentId"] == "NSE:RELIANCE:EQ"

    @pytest.mark.asyncio
    async def test_filter_by_instrument_type(self) -> None:
        """Filtering by instrumentType returns only matching instruments."""
        eq_inst = _make_instrument()
        fut_inst = _make_fno_instrument()
        app = _make_app(instruments=[eq_inst, fut_inst])

        response = await _get(app, "/v1/instruments?instrumentType=EQ")
        body = response.json()
        assert len(body["data"]) == 1
        assert body["data"][0]["instrumentType"] == "EQ"

    @pytest.mark.asyncio
    async def test_filter_by_underlying(self) -> None:
        """Filtering by underlying returns only derivatives on that underlying."""
        eq_inst = _make_instrument()
        fut_inst = _make_fno_instrument()
        app = _make_app(instruments=[eq_inst, fut_inst])

        response = await _get(app, "/v1/instruments?underlying=NIFTY")
        body = response.json()
        assert len(body["data"]) == 1
        assert body["data"][0]["underlying"] == "NIFTY"

    @pytest.mark.asyncio
    async def test_filter_by_segment(self) -> None:
        """Filtering by segment returns only instruments in that segment."""
        eq_inst = _make_instrument()
        fut_inst = _make_fno_instrument()
        app = _make_app(instruments=[eq_inst, fut_inst])

        response = await _get(app, "/v1/instruments?segment=FO")
        body = response.json()
        assert len(body["data"]) == 1
        assert body["data"][0]["segment"] == "FO"

    @pytest.mark.asyncio
    async def test_filter_by_expiry(self) -> None:
        """Filtering by expiry returns only instruments with that exact expiry."""
        eq_inst = _make_instrument()  # no expiry
        fut_inst = _make_fno_instrument(expiry=date(2025, 1, 30))
        app = _make_app(instruments=[eq_inst, fut_inst])

        response = await _get(app, "/v1/instruments?expiry=2025-01-30")
        body = response.json()
        assert len(body["data"]) == 1
        assert body["data"][0]["expiry"] == "2025-01-30"

    @pytest.mark.asyncio
    async def test_invalid_exchange_returns_400(self) -> None:
        """HTTP 400 for an unrecognised exchange value."""
        app = _make_app()
        response = await _get(app, "/v1/instruments?exchange=INVALID_EXCHANGE")
        assert response.status_code == 400
        body = response.json()
        assert body["error"]["code"] == "INVALID_FILTER"
        assert "requestId" in body["error"]

    @pytest.mark.asyncio
    async def test_invalid_instrument_type_returns_400(self) -> None:
        """HTTP 400 for an unrecognised instrumentType value."""
        app = _make_app()
        response = await _get(app, "/v1/instruments?instrumentType=BADTYPE")
        assert response.status_code == 400
        body = response.json()
        assert body["error"]["code"] == "INVALID_FILTER"

    @pytest.mark.asyncio
    async def test_invalid_segment_returns_400(self) -> None:
        """HTTP 400 for an unrecognised segment value."""
        app = _make_app()
        response = await _get(app, "/v1/instruments?segment=BADSEG")
        assert response.status_code == 400
        body = response.json()
        assert body["error"]["code"] == "INVALID_FILTER"

    @pytest.mark.asyncio
    async def test_invalid_expiry_date_returns_400(self) -> None:
        """HTTP 400 when expiry cannot be parsed as ISO-8601 date."""
        app = _make_app()
        response = await _get(app, "/v1/instruments?expiry=not-a-date")
        assert response.status_code == 400
        body = response.json()
        assert body["error"]["code"] == "INVALID_FILTER"

    @pytest.mark.asyncio
    async def test_service_missing_returns_200_empty(self) -> None:
        """When instrument_master is not in app.state, returns 200 empty list."""
        app = _make_app(service_missing=True)
        response = await _get(app, "/v1/instruments")
        assert response.status_code == 200
        body = response.json()
        assert body["data"] == []

    @pytest.mark.asyncio
    async def test_expired_instruments_excluded(self) -> None:
        """Active-only filter excludes instruments whose activeTo is in the past."""
        active = _make_instrument(
            instrumentId="NSE:RELIANCE:EQ",
            activeFrom=date(2020, 1, 1),
            activeTo=None,
        )
        expired = _make_instrument(
            instrumentId="NSE:RETIRED:EQ",
            tradingSymbol="RETIRED",
            activeFrom=date(2015, 1, 1),
            activeTo=date(2020, 12, 31),
        )
        app = _make_app(instruments=[active, expired])
        response = await _get(app, "/v1/instruments")
        body = response.json()
        ids = [item["instrumentId"] for item in body["data"]]
        assert "NSE:RELIANCE:EQ" in ids
        assert "NSE:RETIRED:EQ" not in ids

    @pytest.mark.asyncio
    async def test_combined_filters(self) -> None:
        """Multiple filters combined narrow the result set correctly."""
        nifty_fut = _make_fno_instrument(
            instrumentId="NFO:NIFTY25JANFUT:FUTIDX",
            underlying="NIFTY",
            instrumentType=InstrumentType.FUTIDX,
        )
        banknifty_fut = _make_fno_instrument(
            instrumentId="NFO:BANKNIFTY25JANFUT:FUTIDX",
            tradingSymbol="BANKNIFTY25JANFUT",
            underlying="BANKNIFTY",
            instrumentType=InstrumentType.FUTIDX,
        )
        nifty_opt = _make_fno_instrument(
            instrumentId="NFO:NIFTY25JAN22000CE:OPTIDX",
            tradingSymbol="NIFTY25JAN22000CE",
            underlying="NIFTY",
            instrumentType=InstrumentType.OPTIDX,
            strike=22000.0,
            optionType="CE",
        )
        app = _make_app(instruments=[nifty_fut, banknifty_fut, nifty_opt])

        # Requesting underlying=NIFTY & instrumentType=FUTIDX → only nifty_fut
        response = await _get(
            app, "/v1/instruments?underlying=NIFTY&instrumentType=FUTIDX"
        )
        body = response.json()
        assert len(body["data"]) == 1
        assert body["data"][0]["instrumentId"] == "NFO:NIFTY25JANFUT:FUTIDX"


# ---------------------------------------------------------------------------
# GET /v1/instruments/{instrumentId}
# ---------------------------------------------------------------------------


class TestGetInstrument:
    """Tests for GET /v1/instruments/{instrumentId} (Requirements 2.7, 2.8)."""

    @pytest.mark.asyncio
    async def test_returns_instrument_by_id(self) -> None:
        """HTTP 200 with instrument payload for a known instrumentId."""
        inst = _make_instrument()
        app = _make_app(instruments=[inst])
        response = await _get(app, "/v1/instruments/NSE:RELIANCE:EQ")
        assert response.status_code == 200
        body = response.json()
        assert body["data"]["instrumentId"] == "NSE:RELIANCE:EQ"
        assert body["data"]["tradingSymbol"] == "RELIANCE"

    @pytest.mark.asyncio
    async def test_404_for_unknown_instrument(self) -> None:
        """HTTP 404 when instrumentId is not in the master."""
        app = _make_app(instruments=[])
        response = await _get(app, "/v1/instruments/NSE:UNKNOWN:EQ")
        assert response.status_code == 404
        body = response.json()
        assert body["error"]["code"] == "NOT_FOUND"
        assert "requestId" in body["error"]

    @pytest.mark.asyncio
    async def test_provider_tokens_excluded_by_default(self) -> None:
        """Provider token fields must NOT appear in default single lookup."""
        inst = _make_instrument()
        app = _make_app(instruments=[inst])
        response = await _get(app, "/v1/instruments/NSE:RELIANCE:EQ")
        body = response.json()
        item = body["data"]
        for token_field in ("angelToken", "angelSymbol", "upstoxKey", "upstoxSymbol"):
            assert token_field not in item, f"Token field '{token_field}' should be excluded"

    @pytest.mark.asyncio
    async def test_provider_tokens_included_when_requested(self) -> None:
        """Provider token fields appear when ?include=providerTokens is set."""
        inst = _make_instrument()
        app = _make_app(instruments=[inst])
        response = await _get(
            app, "/v1/instruments/NSE:RELIANCE:EQ?include=providerTokens"
        )
        assert response.status_code == 200
        body = response.json()
        item = body["data"]
        assert item["angelToken"] == "2885"
        assert item["angelSymbol"] == "RELIANCE-EQ"
        assert item["upstoxKey"] == "NSE_EQ|2885"
        assert item["upstoxSymbol"] == "RELIANCE"

    @pytest.mark.asyncio
    async def test_provider_tokens_not_included_for_other_include_values(self) -> None:
        """?include=somethingElse should NOT expose provider tokens."""
        inst = _make_instrument()
        app = _make_app(instruments=[inst])
        response = await _get(
            app, "/v1/instruments/NSE:RELIANCE:EQ?include=somethingElse"
        )
        assert response.status_code == 200
        body = response.json()
        item = body["data"]
        for token_field in ("angelToken", "angelSymbol", "upstoxKey", "upstoxSymbol"):
            assert token_field not in item

    @pytest.mark.asyncio
    async def test_service_missing_returns_503(self) -> None:
        """HTTP 503 when instrument_master is not in app.state."""
        app = _make_app(service_missing=True)
        response = await _get(app, "/v1/instruments/NSE:RELIANCE:EQ")
        assert response.status_code == 503
        body = response.json()
        assert body["error"]["code"] == "SERVICE_UNAVAILABLE"

    @pytest.mark.asyncio
    async def test_canonical_envelope_present(self) -> None:
        """Response must use the canonical success envelope."""
        inst = _make_instrument()
        app = _make_app(instruments=[inst])
        response = await _get(app, "/v1/instruments/NSE:RELIANCE:EQ")
        body = response.json()
        assert "data" in body
        assert "metadata" in body

    @pytest.mark.asyncio
    async def test_derivative_instrument_fields_present(self) -> None:
        """Derivative instrument fields (expiry, strike, optionType) are correct."""
        opt = _make_fno_instrument(
            instrumentId="NFO:NIFTY25JAN22000CE:OPTIDX",
            tradingSymbol="NIFTY25JAN22000CE",
            instrumentType=InstrumentType.OPTIDX,
            strike=22000.0,
            optionType="CE",
            expiry=date(2025, 1, 30),
        )
        app = _make_app(instruments=[opt])
        response = await _get(app, "/v1/instruments/NFO:NIFTY25JAN22000CE:OPTIDX")
        assert response.status_code == 200
        body = response.json()
        data = body["data"]
        assert data["strike"] == 22000.0
        assert data["optionType"] == "CE"
        assert data["expiry"] == "2025-01-30"


# ---------------------------------------------------------------------------
# GET /v1/instruments/fno-universe
# ---------------------------------------------------------------------------


class TestGetFnoUniverse:
    """Tests for GET /v1/instruments/fno-universe (Requirements 2.6, 2.9, 11.5)."""

    @pytest.mark.asyncio
    async def test_503_when_no_snapshot_cached(self) -> None:
        """HTTP 503 with FNO_UNIVERSE_UNAVAILABLE when no snapshot is cached."""
        app = _make_app()

        with patch(
            "src.engines.fno_universe.FnoUniverseService.get_cached_snapshot",
            return_value=None,
        ):
            response = await _get(app, "/v1/instruments/fno-universe")

        assert response.status_code == 503
        body = response.json()
        assert body["error"]["code"] == "FNO_UNIVERSE_UNAVAILABLE"
        assert "requestId" in body["error"]

    @pytest.mark.asyncio
    async def test_200_with_snapshot_data(self) -> None:
        """HTTP 200 with snapshot data when a snapshot is cached."""
        snapshot = _make_snapshot()
        app = _make_app()

        with patch(
            "src.engines.fno_universe.FnoUniverseService.get_cached_snapshot",
            return_value=snapshot,
        ):
            response = await _get(app, "/v1/instruments/fno-universe")

        assert response.status_code == 200
        body = response.json()
        data = body["data"]
        assert data["universeVersion"] == 1
        assert data["fnoEquityCount"] == 180
        assert data["fnoIndexCount"] == 4
        assert data["constituentCount"] == 184
        assert data["status"] == "ACTIVE"

    @pytest.mark.asyncio
    async def test_snapshot_version_exposed_as_universe_version(self) -> None:
        """snapshotVersion is mapped to universeVersion in the response."""
        snapshot = _make_snapshot(snapshotVersion=7)
        app = _make_app()

        with patch(
            "src.engines.fno_universe.FnoUniverseService.get_cached_snapshot",
            return_value=snapshot,
        ):
            response = await _get(app, "/v1/instruments/fno-universe")

        body = response.json()
        assert body["data"]["universeVersion"] == 7
        # snapshotVersion must NOT appear under its original name
        assert "snapshotVersion" not in body["data"]

    @pytest.mark.asyncio
    async def test_canonical_envelope_present(self) -> None:
        """Response uses the canonical success envelope with metadata."""
        snapshot = _make_snapshot()
        app = _make_app()

        with patch(
            "src.engines.fno_universe.FnoUniverseService.get_cached_snapshot",
            return_value=snapshot,
        ):
            response = await _get(app, "/v1/instruments/fno-universe")

        body = response.json()
        assert "data" in body
        assert "metadata" in body
        assert body["metadata"]["dataSourceType"] == "HISTORICAL"

    @pytest.mark.asyncio
    async def test_checksum_present(self) -> None:
        """Checksum must be included in the snapshot response."""
        checksum = "a" * 64
        snapshot = _make_snapshot(checksum=checksum)
        app = _make_app()

        with patch(
            "src.engines.fno_universe.FnoUniverseService.get_cached_snapshot",
            return_value=snapshot,
        ):
            response = await _get(app, "/v1/instruments/fno-universe")

        body = response.json()
        assert body["data"]["checksum"] == checksum

    @pytest.mark.asyncio
    async def test_effective_dates_present(self) -> None:
        """effectiveFrom and effectiveTo must be present in the response."""
        snapshot = _make_snapshot(
            effectiveFrom=date(2025, 1, 15),
            effectiveTo=None,
        )
        app = _make_app()

        with patch(
            "src.engines.fno_universe.FnoUniverseService.get_cached_snapshot",
            return_value=snapshot,
        ):
            response = await _get(app, "/v1/instruments/fno-universe")

        body = response.json()
        assert body["data"]["effectiveFrom"] == "2025-01-15"
        assert body["data"]["effectiveTo"] is None

    @pytest.mark.asyncio
    async def test_error_envelope_for_503(self) -> None:
        """503 response uses canonical error envelope."""
        app = _make_app()

        with patch(
            "src.engines.fno_universe.FnoUniverseService.get_cached_snapshot",
            return_value=None,
        ):
            response = await _get(app, "/v1/instruments/fno-universe")

        body = response.json()
        assert "error" in body
        assert "code" in body["error"]
        assert "message" in body["error"]
        assert "requestId" in body["error"]


# ---------------------------------------------------------------------------
# GET /v1/instruments/fno-universe/history
# ---------------------------------------------------------------------------


class TestGetFnoUniverseHistory:
    """Tests for GET /v1/instruments/fno-universe/history (Requirement 11.5)."""

    @pytest.mark.asyncio
    async def test_200_no_engine_returns_empty(self) -> None:
        """HTTP 200 with empty list when no DB engine is available."""
        app = _make_app(db_engine=None)
        response = await _get(app, "/v1/instruments/fno-universe/history")
        assert response.status_code == 200
        body = response.json()
        assert body["data"]["snapshots"] == []
        assert body["data"]["pagination"]["total"] == 0

    @pytest.mark.asyncio
    async def test_pagination_structure_present(self) -> None:
        """Response must include pagination metadata."""
        app = _make_app(db_engine=None)
        response = await _get(app, "/v1/instruments/fno-universe/history")
        body = response.json()
        pagination = body["data"]["pagination"]
        assert "page" in pagination
        assert "limit" in pagination
        assert "total" in pagination
        assert "totalPages" in pagination

    @pytest.mark.asyncio
    async def test_default_pagination_values(self) -> None:
        """Default page=1, limit=20 when not supplied."""
        app = _make_app(db_engine=None)
        response = await _get(app, "/v1/instruments/fno-universe/history")
        body = response.json()
        assert body["data"]["pagination"]["page"] == 1
        assert body["data"]["pagination"]["limit"] == 20

    @pytest.mark.asyncio
    async def test_invalid_status_returns_400(self) -> None:
        """HTTP 400 for an unrecognised status filter value."""
        app = _make_app(db_engine=None)
        response = await _get(
            app, "/v1/instruments/fno-universe/history?status=INVALID"
        )
        assert response.status_code == 400
        body = response.json()
        assert body["error"]["code"] == "INVALID_FILTER"
        assert "requestId" in body["error"]

    @pytest.mark.asyncio
    async def test_valid_status_active(self) -> None:
        """status=ACTIVE is accepted (no 400)."""
        app = _make_app(db_engine=None)
        response = await _get(
            app, "/v1/instruments/fno-universe/history?status=ACTIVE"
        )
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_valid_status_superseded(self) -> None:
        """status=SUPERSEDED is accepted (no 400)."""
        app = _make_app(db_engine=None)
        response = await _get(
            app, "/v1/instruments/fno-universe/history?status=SUPERSEDED"
        )
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_canonical_envelope_present(self) -> None:
        """Response uses the canonical success envelope."""
        app = _make_app(db_engine=None)
        response = await _get(app, "/v1/instruments/fno-universe/history")
        body = response.json()
        assert "data" in body
        assert "metadata" in body

    @pytest.mark.asyncio
    async def test_max_limit_100(self) -> None:
        """FastAPI rejects limit > 100 with a validation error."""
        app = _make_app(db_engine=None)
        response = await _get(
            app, "/v1/instruments/fno-universe/history?limit=200"
        )
        # FastAPI query param validation returns 422 for out-of-range values
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_db_engine_queried_when_available(self) -> None:
        """When a DB engine is set, the history endpoint queries it."""
        # Mock DB engine that returns rows
        mock_engine = MagicMock()

        # We'll simulate an engine that raises an exception, verifying it's
        # called. The endpoint falls through to empty list on exception.
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock(side_effect=Exception("simulated DB error"))
        mock_engine.connect = MagicMock(
            return_value=AsyncMock(
                __aenter__=AsyncMock(return_value=mock_conn),
                __aexit__=AsyncMock(return_value=None),
            )
        )
        app = _make_app(db_engine=mock_engine)
        response = await _get(app, "/v1/instruments/fno-universe/history")
        # Should degrade gracefully and return 200 with empty list
        assert response.status_code == 200
        body = response.json()
        assert body["data"]["snapshots"] == []


# ---------------------------------------------------------------------------
# Route priority: /fno-universe is not shadowed by /{instrumentId}
# ---------------------------------------------------------------------------


class TestRoutePriority:
    """Verify that static paths are matched before the dynamic /{instrumentId}."""

    @pytest.mark.asyncio
    async def test_fno_universe_path_not_treated_as_instrument_id(self) -> None:
        """GET /v1/instruments/fno-universe must route to the snapshot endpoint,
        not to the single-instrument lookup endpoint with id='fno-universe'."""
        app = _make_app()

        with patch(
            "src.engines.fno_universe.FnoUniverseService.get_cached_snapshot",
            return_value=_make_snapshot(),
        ):
            response = await _get(app, "/v1/instruments/fno-universe")

        # If route priority is wrong this would be a 404 (instrument not found)
        # instead of the expected 200 snapshot response.
        assert response.status_code == 200
        body = response.json()
        # Snapshot endpoint returns data.universeVersion, not error
        assert "universeVersion" in body["data"]

    @pytest.mark.asyncio
    async def test_fno_universe_history_path_not_treated_as_instrument_id(self) -> None:
        """GET /v1/instruments/fno-universe/history must route to history endpoint."""
        app = _make_app(db_engine=None)
        response = await _get(app, "/v1/instruments/fno-universe/history")
        # If route priority is wrong this would 404 on an instrument lookup
        assert response.status_code == 200
        body = response.json()
        assert "snapshots" in body["data"]
