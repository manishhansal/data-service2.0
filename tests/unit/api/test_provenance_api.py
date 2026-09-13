"""
Unit tests for the provenance and lineage API endpoints (Task 10.4).

Covers:
    GET  /v1/provenance/{observation_id}
    GET  /v1/provenance/instrument/{instrument_id}
    POST /v1/provenance
    PUT/PATCH/DELETE /v1/provenance/{observation_id}  → 405
    GET  /v1/lineage/trade/{trade_id}
    GET  /v1/lineage/strategy/{strategy_id}
    GET  /v1/lineage/instrument/{instrument_id}

Uses an in-process ASGI test client (httpx.AsyncClient via ASGITransport) so
no live Redis or PostgreSQL is needed.

Requirements: 8.4, 8.5, 8.7, 8.8, 8.9
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.api.provenance import router as provenance_router
from src.forensics.trade_forensics import TradeForensicsStore
from src.stores.lineage_store import LineageStore
from src.core.schemas.provenance import DataProvenance, DataSource, ProvenanceFactory


# ---------------------------------------------------------------------------
# Test application factory
# ---------------------------------------------------------------------------


def _make_app(
    *,
    lineage_store: LineageStore | None = None,
    forensics_store: TradeForensicsStore | None = None,
) -> FastAPI:
    """Build a minimal FastAPI app with only the provenance router and optional
    pre-seeded stores on app.state."""
    app = FastAPI()
    app.include_router(provenance_router, prefix="/v1")

    if lineage_store is not None:
        app.state.lineage_store = lineage_store
    if forensics_store is not None:
        app.state.trade_forensics_store = forensics_store
    # Do NOT set db_engine — cache-only mode
    return app


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


async def _get(app: FastAPI, path: str, **params: Any) -> Any:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path, params=params)


async def _post(app: FastAPI, path: str, *, json: Any = None) -> Any:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(path, json=json or {})


async def _put(app: FastAPI, path: str) -> Any:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.put(path, json={})


async def _patch(app: FastAPI, path: str) -> Any:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.patch(path, json={})


async def _delete(app: FastAPI, path: str) -> Any:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.delete(path)


# ---------------------------------------------------------------------------
# Fixtures — pre-built stores with data
# ---------------------------------------------------------------------------


def _make_provenance(**kwargs: Any) -> DataProvenance:
    base = dict(
        instrument_id="NSE:NIFTY50:IDX",
        exchange="NSE",
        primary_provider=DataSource.ANGEL_ONE,
    )
    base.update(kwargs)
    return ProvenanceFactory.create(**base)  # type: ignore[arg-type]


async def _seeded_lineage_store(n: int = 3) -> tuple[LineageStore, list[DataProvenance]]:
    """Return a LineageStore with *n* records already inserted."""
    store = LineageStore(max_cache_size=10_000)
    records: list[DataProvenance] = []
    for i in range(n):
        prov = _make_provenance(
            instrument_id=f"NSE:STOCK{i}:EQ",
            exchange="NSE",
        )
        await store.put(str(prov.dataObservationId), prov)
        records.append(prov)
    return store, records


def _seeded_forensics_store(n: int = 3) -> tuple[TradeForensicsStore, list[Any]]:
    """Return a TradeForensicsStore with *n* records for a fixed strategy/instrument."""
    store = TradeForensicsStore()
    records = []
    for i in range(n):
        rec = store.record(
            trade_id=str(uuid.uuid4()),
            observation_ids=[str(uuid.uuid4())],
            strategy_id="strat-alpha",
            confidence_score=80,
            signal_allowed=True,
            instrument_id="NSE:NIFTY50:IDX",
            exchange="NSE",
        )
        records.append(rec)
    return store, records


# ===========================================================================
# GET /v1/provenance/{observation_id}
# ===========================================================================


class TestGetProvenance:
    """Tests for GET /v1/provenance/{observation_id}."""

    @pytest.mark.asyncio
    async def test_found_record_returns_200(self) -> None:
        store, records = await _seeded_lineage_store(1)
        obs_id = str(records[0].dataObservationId)
        app = _make_app(lineage_store=store)

        resp = await _get(app, f"/v1/provenance/{obs_id}")

        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_response_envelope_structure(self) -> None:
        store, records = await _seeded_lineage_store(1)
        obs_id = str(records[0].dataObservationId)
        app = _make_app(lineage_store=store)

        resp = await _get(app, f"/v1/provenance/{obs_id}")
        body = resp.json()

        assert "data" in body
        assert "metadata" in body
        assert "requestedAt" in body["metadata"]
        assert "dataSourceType" in body["metadata"]

    @pytest.mark.asyncio
    async def test_data_contains_observation_id(self) -> None:
        store, records = await _seeded_lineage_store(1)
        obs_id = str(records[0].dataObservationId)
        app = _make_app(lineage_store=store)

        resp = await _get(app, f"/v1/provenance/{obs_id}")
        data = resp.json()["data"]

        assert data["dataObservationId"] == obs_id

    @pytest.mark.asyncio
    async def test_data_has_key_provenance_fields(self) -> None:
        store, records = await _seeded_lineage_store(1)
        obs_id = str(records[0].dataObservationId)
        app = _make_app(lineage_store=store)

        resp = await _get(app, f"/v1/provenance/{obs_id}")
        data = resp.json()["data"]

        # Required provenance fields per Requirement 8.2
        for field in (
            "dataObservationId",
            "source",
            "isFallback",
            "normalisationVersion",
            "dataTrustStatus",
        ):
            assert field in data, f"Expected field '{field}' in provenance response"

    @pytest.mark.asyncio
    async def test_unknown_id_returns_404(self) -> None:
        app = _make_app(lineage_store=LineageStore())
        fake_id = str(uuid.uuid4())

        resp = await _get(app, f"/v1/provenance/{fake_id}")

        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_404_has_canonical_error_envelope(self) -> None:
        app = _make_app(lineage_store=LineageStore())
        resp = await _get(app, f"/v1/provenance/{uuid.uuid4()}")
        err = resp.json()["error"]

        assert err["code"] == "OBSERVATION_NOT_FOUND"
        assert "message" in err
        assert "requestId" in err

    @pytest.mark.asyncio
    async def test_data_source_type_is_historical(self) -> None:
        store, records = await _seeded_lineage_store(1)
        obs_id = str(records[0].dataObservationId)
        app = _make_app(lineage_store=store)

        resp = await _get(app, f"/v1/provenance/{obs_id}")

        assert resp.json()["metadata"]["dataSourceType"] == "HISTORICAL"

    @pytest.mark.asyncio
    async def test_lazy_store_creation_when_no_state(self) -> None:
        """When app.state has no lineage_store, one is created on demand."""
        app = _make_app()  # no store in state
        resp = await _get(app, f"/v1/provenance/{uuid.uuid4()}")
        # Should return 404 (not found), not 500
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_multiple_records_are_independently_retrievable(self) -> None:
        store, records = await _seeded_lineage_store(3)
        app = _make_app(lineage_store=store)

        for rec in records:
            obs_id = str(rec.dataObservationId)
            resp = await _get(app, f"/v1/provenance/{obs_id}")
            assert resp.status_code == 200
            assert resp.json()["data"]["dataObservationId"] == obs_id


# ===========================================================================
# GET /v1/provenance/instrument/{instrument_id}
# ===========================================================================


class TestGetProvenanceByInstrument:
    """Tests for GET /v1/provenance/instrument/{instrument_id}."""

    @pytest.mark.asyncio
    async def test_returns_200_with_records(self) -> None:
        store = LineageStore(max_cache_size=10_000)
        prov = _make_provenance(instrument_id="NSE:RELIANCE:EQ", exchange="NSE")
        await store.put(str(prov.dataObservationId), prov)
        app = _make_app(lineage_store=store)

        resp = await _get(app, "/v1/provenance/instrument/NSE:RELIANCE:EQ")

        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_response_envelope_has_data_and_metadata(self) -> None:
        store = LineageStore(max_cache_size=10_000)
        prov = _make_provenance(instrument_id="NSE:RELIANCE:EQ", exchange="NSE")
        await store.put(str(prov.dataObservationId), prov)
        app = _make_app(lineage_store=store)

        resp = await _get(app, "/v1/provenance/instrument/NSE:RELIANCE:EQ")
        body = resp.json()

        assert "data" in body
        assert "metadata" in body

    @pytest.mark.asyncio
    async def test_metadata_contains_store_size_and_total_recorded(self) -> None:
        store = LineageStore(max_cache_size=10_000)
        prov = _make_provenance(instrument_id="NSE:RELIANCE:EQ", exchange="NSE")
        await store.put(str(prov.dataObservationId), prov)
        app = _make_app(lineage_store=store)

        resp = await _get(app, "/v1/provenance/instrument/NSE:RELIANCE:EQ")
        meta = resp.json()["metadata"]

        assert "storeSize" in meta
        assert "totalRecorded" in meta
        assert isinstance(meta["storeSize"], int)
        assert isinstance(meta["totalRecorded"], int)

    @pytest.mark.asyncio
    async def test_returns_empty_list_for_unknown_instrument(self) -> None:
        app = _make_app(lineage_store=LineageStore())

        resp = await _get(app, "/v1/provenance/instrument/UNKNOWN:IDX")

        assert resp.status_code == 200
        assert resp.json()["data"] == []

    @pytest.mark.asyncio
    async def test_limit_defaults_to_100(self) -> None:
        store = LineageStore(max_cache_size=10_000)
        for _ in range(5):
            prov = _make_provenance(instrument_id="NSE:NIFTY:IDX", exchange="NSE")
            await store.put(str(prov.dataObservationId), prov)
        app = _make_app(lineage_store=store)

        resp = await _get(app, "/v1/provenance/instrument/NSE:NIFTY:IDX")
        meta = resp.json()["metadata"]

        assert meta["limit"] == 100

    @pytest.mark.asyncio
    async def test_custom_limit_respected(self) -> None:
        store = LineageStore(max_cache_size=10_000)
        for _ in range(10):
            prov = _make_provenance(instrument_id="NSE:NIFTY:IDX", exchange="NSE")
            await store.put(str(prov.dataObservationId), prov)
        app = _make_app(lineage_store=store)

        resp = await _get(app, "/v1/provenance/instrument/NSE:NIFTY:IDX", limit=3)

        assert resp.status_code == 200
        assert len(resp.json()["data"]) <= 3

    @pytest.mark.asyncio
    async def test_limit_below_1_returns_422(self) -> None:
        app = _make_app(lineage_store=LineageStore())
        resp = await _get(app, "/v1/provenance/instrument/NSE:NIFTY:IDX", limit=0)
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_limit_above_1000_returns_422(self) -> None:
        app = _make_app(lineage_store=LineageStore())
        resp = await _get(app, "/v1/provenance/instrument/NSE:NIFTY:IDX", limit=9999)
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_metadata_count_matches_data_length(self) -> None:
        store = LineageStore(max_cache_size=10_000)
        for _ in range(4):
            prov = _make_provenance(instrument_id="NSE:HDFC:EQ", exchange="NSE")
            await store.put(str(prov.dataObservationId), prov)
        app = _make_app(lineage_store=store)

        resp = await _get(app, "/v1/provenance/instrument/NSE:HDFC:EQ")
        body = resp.json()

        assert body["metadata"]["count"] == len(body["data"])


# ===========================================================================
# POST /v1/provenance
# ===========================================================================


class TestCreateProvenance:
    """Tests for POST /v1/provenance."""

    def _valid_body(self, **overrides: Any) -> dict[str, Any]:
        body: dict[str, Any] = {
            "instrumentId": "NSE:NIFTY50:IDX",
            "exchange": "NSE",
            "primaryProvider": "ANGEL_ONE",
        }
        body.update(overrides)
        return body

    @pytest.mark.asyncio
    async def test_valid_body_returns_201(self) -> None:
        app = _make_app(lineage_store=LineageStore())
        resp = await _post(app, "/v1/provenance", json=self._valid_body())
        assert resp.status_code == 201

    @pytest.mark.asyncio
    async def test_response_envelope_structure(self) -> None:
        app = _make_app(lineage_store=LineageStore())
        resp = await _post(app, "/v1/provenance", json=self._valid_body())
        body = resp.json()

        assert "data" in body
        assert "metadata" in body

    @pytest.mark.asyncio
    async def test_created_record_has_observation_id(self) -> None:
        app = _make_app(lineage_store=LineageStore())
        resp = await _post(app, "/v1/provenance", json=self._valid_body())
        data = resp.json()["data"]

        assert "dataObservationId" in data
        # Must be a valid UUID
        uuid.UUID(data["dataObservationId"])

    @pytest.mark.asyncio
    async def test_created_record_has_expected_fields(self) -> None:
        app = _make_app(lineage_store=LineageStore())
        resp = await _post(app, "/v1/provenance", json=self._valid_body())
        data = resp.json()["data"]

        assert data["exchange"] == "NSE"
        assert data["instrumentId"] == "NSE:NIFTY50:IDX"
        assert data["source"] == "ANGEL_ONE"

    @pytest.mark.asyncio
    async def test_created_record_is_retrievable_by_get(self) -> None:
        store = LineageStore()
        app = _make_app(lineage_store=store)

        resp = await _post(app, "/v1/provenance", json=self._valid_body())
        obs_id = resp.json()["data"]["dataObservationId"]

        get_resp = await _get(app, f"/v1/provenance/{obs_id}")
        assert get_resp.status_code == 200
        assert get_resp.json()["data"]["dataObservationId"] == obs_id

    @pytest.mark.asyncio
    async def test_missing_instrument_id_returns_400(self) -> None:
        app = _make_app(lineage_store=LineageStore())
        body = {"exchange": "NSE", "primaryProvider": "ANGEL_ONE"}
        resp = await _post(app, "/v1/provenance", json=body)
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "MISSING_FIELD"

    @pytest.mark.asyncio
    async def test_missing_exchange_returns_400(self) -> None:
        app = _make_app(lineage_store=LineageStore())
        body = {"instrumentId": "NSE:NIFTY:IDX", "primaryProvider": "ANGEL_ONE"}
        resp = await _post(app, "/v1/provenance", json=body)
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "MISSING_FIELD"

    @pytest.mark.asyncio
    async def test_missing_provider_returns_400(self) -> None:
        app = _make_app(lineage_store=LineageStore())
        body = {"instrumentId": "NSE:NIFTY:IDX", "exchange": "NSE"}
        resp = await _post(app, "/v1/provenance", json=body)
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "MISSING_FIELD"

    @pytest.mark.asyncio
    async def test_invalid_source_type_returns_400(self) -> None:
        app = _make_app(lineage_store=LineageStore())
        body = self._valid_body(sourceType="INVALID_TYPE")
        resp = await _post(app, "/v1/provenance", json=body)
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "INVALID_FIELD"

    @pytest.mark.asyncio
    async def test_optional_fields_are_accepted(self) -> None:
        app = _make_app(lineage_store=LineageStore())
        body = self._valid_body(
            sourceVersion="v2",
            isFallback=True,
            fallbackReason="Primary unavailable",
            intervalStr="1m",
            sessionDate="2025-01-15",
            authenticated=True,
            rowCount=390,
        )
        resp = await _post(app, "/v1/provenance", json=body)
        assert resp.status_code == 201
        data = resp.json()["data"]
        assert data["isFallback"] is True
        assert data["rowCount"] == 390

    @pytest.mark.asyncio
    async def test_source_field_as_alias_for_primary_provider(self) -> None:
        """'source' key is accepted as alias for 'primaryProvider'."""
        app = _make_app(lineage_store=LineageStore())
        body = {"instrumentId": "NSE:NIFTY:IDX", "exchange": "NSE", "source": "UPSTOX"}
        resp = await _post(app, "/v1/provenance", json=body)
        assert resp.status_code == 201

    @pytest.mark.asyncio
    async def test_two_created_records_have_different_observation_ids(self) -> None:
        app = _make_app(lineage_store=LineageStore())
        r1 = (await _post(app, "/v1/provenance", json=self._valid_body())).json()
        r2 = (await _post(app, "/v1/provenance", json=self._valid_body())).json()
        assert r1["data"]["dataObservationId"] != r2["data"]["dataObservationId"]


# ===========================================================================
# Mutation endpoints → HTTP 405 (Requirement 8.9)
# ===========================================================================


class TestProvenanceImmutability:
    """Verify that PUT, PATCH, DELETE on provenance records return HTTP 405."""

    @pytest.mark.asyncio
    async def test_put_returns_405(self) -> None:
        app = _make_app()
        resp = await _put(app, f"/v1/provenance/{uuid.uuid4()}")
        assert resp.status_code == 405

    @pytest.mark.asyncio
    async def test_patch_returns_405(self) -> None:
        app = _make_app()
        resp = await _patch(app, f"/v1/provenance/{uuid.uuid4()}")
        assert resp.status_code == 405

    @pytest.mark.asyncio
    async def test_delete_returns_405(self) -> None:
        app = _make_app()
        resp = await _delete(app, f"/v1/provenance/{uuid.uuid4()}")
        assert resp.status_code == 405

    @pytest.mark.asyncio
    async def test_405_response_has_canonical_error_envelope(self) -> None:
        app = _make_app()
        resp = await _put(app, f"/v1/provenance/{uuid.uuid4()}")
        err = resp.json()["error"]

        assert err["code"] == "PROVENANCE_IMMUTABLE"
        assert "message" in err
        assert "requestId" in err

    @pytest.mark.asyncio
    async def test_existing_record_unchanged_after_mutation_attempt(self) -> None:
        store = LineageStore()
        prov = _make_provenance(instrument_id="NSE:NIFTY:IDX", exchange="NSE")
        await store.put(str(prov.dataObservationId), prov)
        app = _make_app(lineage_store=store)
        obs_id = str(prov.dataObservationId)

        await _put(app, f"/v1/provenance/{obs_id}")

        # Original record must still be retrievable and unchanged
        get_resp = await _get(app, f"/v1/provenance/{obs_id}")
        assert get_resp.status_code == 200
        assert get_resp.json()["data"]["dataObservationId"] == obs_id


# ===========================================================================
# GET /v1/lineage/trade/{trade_id}
# ===========================================================================


class TestGetTradeForensics:
    """Tests for GET /v1/lineage/trade/{trade_id}."""

    @pytest.mark.asyncio
    async def test_found_record_returns_200(self) -> None:
        store, records = _seeded_forensics_store(1)
        trade_id = records[0].tradeId
        app = _make_app(forensics_store=store)

        resp = await _get(app, f"/v1/lineage/trade/{trade_id}")

        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_response_envelope_structure(self) -> None:
        store, records = _seeded_forensics_store(1)
        trade_id = records[0].tradeId
        app = _make_app(forensics_store=store)

        resp = await _get(app, f"/v1/lineage/trade/{trade_id}")
        body = resp.json()

        assert "data" in body
        assert "metadata" in body

    @pytest.mark.asyncio
    async def test_data_contains_trade_id(self) -> None:
        store, records = _seeded_forensics_store(1)
        trade_id = records[0].tradeId
        app = _make_app(forensics_store=store)

        resp = await _get(app, f"/v1/lineage/trade/{trade_id}")

        assert resp.json()["data"]["tradeId"] == trade_id

    @pytest.mark.asyncio
    async def test_data_has_required_forensics_fields(self) -> None:
        store, records = _seeded_forensics_store(1)
        trade_id = records[0].tradeId
        app = _make_app(forensics_store=store)

        resp = await _get(app, f"/v1/lineage/trade/{trade_id}")
        data = resp.json()["data"]

        for field in (
            "tradeId",
            "strategyId",
            "instrumentId",
            "exchange",
            "observationIds",
            "dataConfidenceAtExecution",
            "signalEngineAllowed",
            "executedAt",
        ):
            assert field in data, f"Expected field '{field}' in forensics response"

    @pytest.mark.asyncio
    async def test_unknown_trade_id_returns_404(self) -> None:
        app = _make_app(forensics_store=TradeForensicsStore())

        resp = await _get(app, "/v1/lineage/trade/nonexistent-trade-id")

        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_404_has_canonical_error_envelope(self) -> None:
        app = _make_app(forensics_store=TradeForensicsStore())
        resp = await _get(app, "/v1/lineage/trade/no-such-trade")
        err = resp.json()["error"]

        assert err["code"] == "TRADE_NOT_FOUND"
        assert "message" in err
        assert "requestId" in err

    @pytest.mark.asyncio
    async def test_data_source_type_is_historical(self) -> None:
        store, records = _seeded_forensics_store(1)
        app = _make_app(forensics_store=store)

        resp = await _get(app, f"/v1/lineage/trade/{records[0].tradeId}")

        assert resp.json()["metadata"]["dataSourceType"] == "HISTORICAL"

    @pytest.mark.asyncio
    async def test_lazy_store_creation(self) -> None:
        """When app.state has no forensics_store, one is created on demand."""
        app = _make_app()
        resp = await _get(app, "/v1/lineage/trade/some-trade-id")
        # Should return 404, not 500
        assert resp.status_code == 404


# ===========================================================================
# GET /v1/lineage/strategy/{strategy_id}
# ===========================================================================


class TestGetForensicsByStrategy:
    """Tests for GET /v1/lineage/strategy/{strategy_id}."""

    @pytest.mark.asyncio
    async def test_returns_200_with_records(self) -> None:
        store, _ = _seeded_forensics_store(3)
        app = _make_app(forensics_store=store)

        resp = await _get(app, "/v1/lineage/strategy/strat-alpha")

        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_data_is_list(self) -> None:
        store, _ = _seeded_forensics_store(3)
        app = _make_app(forensics_store=store)

        resp = await _get(app, "/v1/lineage/strategy/strat-alpha")

        assert isinstance(resp.json()["data"], list)
        assert len(resp.json()["data"]) == 3

    @pytest.mark.asyncio
    async def test_returns_empty_list_for_unknown_strategy(self) -> None:
        app = _make_app(forensics_store=TradeForensicsStore())

        resp = await _get(app, "/v1/lineage/strategy/unknown-strategy")

        assert resp.status_code == 200
        assert resp.json()["data"] == []

    @pytest.mark.asyncio
    async def test_default_limit_is_50(self) -> None:
        store, _ = _seeded_forensics_store(2)
        app = _make_app(forensics_store=store)

        resp = await _get(app, "/v1/lineage/strategy/strat-alpha")
        meta = resp.json()["metadata"]

        assert meta["limit"] == 50

    @pytest.mark.asyncio
    async def test_custom_limit_applied(self) -> None:
        store, _ = _seeded_forensics_store(10)
        # Add all 10 to the same strategy
        for _ in range(5):
            store.record(
                trade_id=str(uuid.uuid4()),
                observation_ids=[str(uuid.uuid4())],
                strategy_id="strat-alpha",
                confidence_score=70,
                signal_allowed=True,
                instrument_id="NSE:NIFTY:IDX",
                exchange="NSE",
            )
        app = _make_app(forensics_store=store)

        resp = await _get(app, "/v1/lineage/strategy/strat-alpha", limit=2)

        assert len(resp.json()["data"]) <= 2

    @pytest.mark.asyncio
    async def test_metadata_contains_strategy_id(self) -> None:
        store, _ = _seeded_forensics_store(1)
        app = _make_app(forensics_store=store)

        resp = await _get(app, "/v1/lineage/strategy/strat-alpha")

        assert resp.json()["metadata"]["strategy_id"] == "strat-alpha"

    @pytest.mark.asyncio
    async def test_limit_below_1_returns_422(self) -> None:
        app = _make_app(forensics_store=TradeForensicsStore())
        resp = await _get(app, "/v1/lineage/strategy/strat-alpha", limit=0)
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_limit_above_1000_returns_422(self) -> None:
        app = _make_app(forensics_store=TradeForensicsStore())
        resp = await _get(app, "/v1/lineage/strategy/strat-alpha", limit=9999)
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_metadata_count_matches_data_length(self) -> None:
        store, _ = _seeded_forensics_store(3)
        app = _make_app(forensics_store=store)

        resp = await _get(app, "/v1/lineage/strategy/strat-alpha")
        body = resp.json()

        assert body["metadata"]["count"] == len(body["data"])


# ===========================================================================
# GET /v1/lineage/instrument/{instrument_id}
# ===========================================================================


class TestGetForensicsByInstrument:
    """Tests for GET /v1/lineage/instrument/{instrument_id}."""

    @pytest.mark.asyncio
    async def test_returns_200_with_records(self) -> None:
        store, _ = _seeded_forensics_store(3)
        app = _make_app(forensics_store=store)

        resp = await _get(app, "/v1/lineage/instrument/NSE:NIFTY50:IDX")

        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_data_is_list_matching_instrument(self) -> None:
        store, _ = _seeded_forensics_store(3)
        app = _make_app(forensics_store=store)

        resp = await _get(app, "/v1/lineage/instrument/NSE:NIFTY50:IDX")
        data = resp.json()["data"]

        assert isinstance(data, list)
        # All records were created for NSE:NIFTY50:IDX
        assert len(data) == 3
        for rec in data:
            assert rec["instrumentId"] == "NSE:NIFTY50:IDX"

    @pytest.mark.asyncio
    async def test_returns_empty_for_unknown_instrument(self) -> None:
        app = _make_app(forensics_store=TradeForensicsStore())

        resp = await _get(app, "/v1/lineage/instrument/NSE:UNKNOWN:EQ")

        assert resp.status_code == 200
        assert resp.json()["data"] == []

    @pytest.mark.asyncio
    async def test_default_limit_is_50(self) -> None:
        store, _ = _seeded_forensics_store(2)
        app = _make_app(forensics_store=store)

        resp = await _get(app, "/v1/lineage/instrument/NSE:NIFTY50:IDX")

        assert resp.json()["metadata"]["limit"] == 50

    @pytest.mark.asyncio
    async def test_metadata_contains_instrument_id(self) -> None:
        store, _ = _seeded_forensics_store(1)
        app = _make_app(forensics_store=store)

        resp = await _get(app, "/v1/lineage/instrument/NSE:NIFTY50:IDX")

        assert resp.json()["metadata"]["instrument_id"] == "NSE:NIFTY50:IDX"

    @pytest.mark.asyncio
    async def test_limit_validation_below_1_returns_422(self) -> None:
        app = _make_app(forensics_store=TradeForensicsStore())
        resp = await _get(app, "/v1/lineage/instrument/NSE:NIFTY50:IDX", limit=0)
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_metadata_count_matches_data_length(self) -> None:
        store, _ = _seeded_forensics_store(3)
        app = _make_app(forensics_store=store)

        resp = await _get(app, "/v1/lineage/instrument/NSE:NIFTY50:IDX")
        body = resp.json()

        assert body["metadata"]["count"] == len(body["data"])

    @pytest.mark.asyncio
    async def test_custom_limit_applied(self) -> None:
        store, _ = _seeded_forensics_store(5)
        app = _make_app(forensics_store=store)

        resp = await _get(app, "/v1/lineage/instrument/NSE:NIFTY50:IDX", limit=2)

        assert len(resp.json()["data"]) <= 2


# ===========================================================================
# Response format invariants
# ===========================================================================


class TestResponseFormatInvariants:
    """Cross-cutting tests that verify metadata structure for all endpoints."""

    @pytest.mark.asyncio
    async def test_all_success_responses_have_requested_at_z_suffix(self) -> None:
        """requestedAt must be UTC ISO-8601 with Z suffix."""
        store, records = await _seeded_lineage_store(1)
        fstore, frecords = _seeded_forensics_store(1)
        app = _make_app(lineage_store=store, forensics_store=fstore)
        obs_id = str(records[0].dataObservationId)

        endpoints = [
            f"/v1/provenance/{obs_id}",
            "/v1/provenance/instrument/NSE:STOCK0:EQ",
            f"/v1/lineage/trade/{frecords[0].tradeId}",
            "/v1/lineage/strategy/strat-alpha",
            "/v1/lineage/instrument/NSE:NIFTY50:IDX",
        ]
        for path in endpoints:
            resp = await _get(app, path)
            assert resp.status_code in (200, 201)
            requested_at = resp.json()["metadata"]["requestedAt"]
            assert requested_at.endswith("Z"), (
                f"requestedAt for {path} must end with 'Z', got {requested_at!r}"
            )

    @pytest.mark.asyncio
    async def test_all_error_responses_have_request_id(self) -> None:
        """All error responses must include requestId."""
        app = _make_app(
            lineage_store=LineageStore(),
            forensics_store=TradeForensicsStore(),
        )
        error_endpoints = [
            "/v1/provenance/nonexistent-id",
            "/v1/lineage/trade/nonexistent-trade",
        ]
        for path in error_endpoints:
            resp = await _get(app, path)
            assert "error" in resp.json()
            assert "requestId" in resp.json()["error"], (
                f"requestId missing in error for {path}"
            )
