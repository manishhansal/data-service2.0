"""
tests/unit/core/schemas/test_provenance.py

Unit tests for src/core/schemas/provenance.py.

Covers:
- DataProvenance model field validation and defaults
- DataProvenance immutability (frozen=True)
- dataObservationId is a UUID v4
- ProvenanceFactory.create() produces fully-populated records
- ProvenanceFactory.create() responseHash is correct SHA-256 of raw payload
- ProvenanceFactory.from_tick() extracts fields from a tick dict
- ProvenanceFactory.from_tick() raises KeyError for missing required keys
- Two successive create() calls produce distinct dataObservationIds
- Fallback fields default to expected values when not supplied

Requirements: 8.1, 8.2
"""

from __future__ import annotations

import hashlib
from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError

from src.core.schemas.provenance import (
    DataProvenance,
    DataSource,
    DataTrustStatus,
    ProvenanceFactory,
    ProviderSourceType,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_uuid4(value: UUID | str) -> bool:
    """Return True when *value* is a well-formed UUID v4."""
    try:
        parsed = UUID(str(value)) if isinstance(value, str) else value
    except ValueError:
        return False
    return parsed.version == 4


def _sha256(raw: bytes | str) -> str:
    if isinstance(raw, str):
        raw = raw.encode()
    return hashlib.sha256(raw).hexdigest()


# ===========================================================================
# DataProvenance model — field validation and defaults
# ===========================================================================


class TestDataProvenanceDefaults:
    """Tests for default field values on DataProvenance."""

    def test_can_construct_with_no_args(self) -> None:
        """DataProvenance has all-optional fields; default construction must not raise."""
        prov = DataProvenance()
        assert prov is not None

    def test_observation_id_is_uuid4_by_default(self) -> None:
        prov = DataProvenance()
        assert _is_uuid4(prov.dataObservationId)

    def test_each_instance_has_unique_observation_id(self) -> None:
        """default_factory=uuid4 must produce a different UUID for every instance."""
        prov_a = DataProvenance()
        prov_b = DataProvenance()
        assert prov_a.dataObservationId != prov_b.dataObservationId

    def test_source_defaults_to_unknown(self) -> None:
        prov = DataProvenance()
        assert prov.source == DataSource.UNKNOWN

    def test_is_fallback_defaults_to_false(self) -> None:
        prov = DataProvenance()
        assert prov.isFallback is False

    def test_fallback_reason_defaults_to_none(self) -> None:
        prov = DataProvenance()
        assert prov.fallbackReason is None

    def test_source_chain_defaults_to_empty_list(self) -> None:
        prov = DataProvenance()
        assert prov.sourceChain == []

    def test_validation_applied_defaults_to_true(self) -> None:
        prov = DataProvenance()
        assert prov.validationApplied is True

    def test_normalisation_version_defaults_to_2_0_0(self) -> None:
        prov = DataProvenance()
        assert prov.normalisationVersion == "2.0.0"

    def test_data_trust_status_defaults_to_trusted(self) -> None:
        prov = DataProvenance()
        assert prov.dataTrustStatus == DataTrustStatus.TRUSTED

    def test_authenticated_defaults_to_false(self) -> None:
        prov = DataProvenance()
        assert prov.authenticated is False

    def test_optional_fields_default_to_none(self) -> None:
        prov = DataProvenance()
        for field in (
            "sourceVersion", "eventTimeMs", "receivedAtMs", "availableAtMs",
            "instrumentId", "exchange", "intervalStr", "sessionDate",
            "provider", "sourceType", "datasetKey", "dataAsOf",
            "responseHash", "fromTs", "toTs", "datasetVersion", "rowCount",
        ):
            assert getattr(prov, field) is None, f"Expected {field!r} to be None"

    def test_custom_source_accepted(self) -> None:
        prov = DataProvenance(source=DataSource.ANGEL_ONE)
        assert prov.source == DataSource.ANGEL_ONE

    def test_all_data_sources_accepted(self) -> None:
        for member in DataSource:
            prov = DataProvenance(source=member)
            assert prov.source == member

    def test_is_fallback_with_fallback_reason(self) -> None:
        prov = DataProvenance(isFallback=True, fallbackReason="primary_circuit_open")
        assert prov.isFallback is True
        assert prov.fallbackReason == "primary_circuit_open"

    def test_source_chain_populated(self) -> None:
        chain = ["angel_one", "openchart"]
        prov = DataProvenance(sourceChain=chain)
        assert prov.sourceChain == chain


# ===========================================================================
# DataProvenance model — immutability
# ===========================================================================


class TestDataProvenanceImmutability:
    """The model is frozen — no field may be mutated after construction."""

    def test_cannot_mutate_observation_id(self) -> None:
        prov = DataProvenance()
        with pytest.raises(Exception):
            prov.dataObservationId = "new-value"  # type: ignore[misc]

    def test_cannot_mutate_source(self) -> None:
        prov = DataProvenance(source=DataSource.ANGEL_ONE)
        with pytest.raises(Exception):
            prov.source = DataSource.BINANCE  # type: ignore[misc]

    def test_cannot_mutate_normalisation_version(self) -> None:
        prov = DataProvenance()
        with pytest.raises(Exception):
            prov.normalisationVersion = "9.9.9"  # type: ignore[misc]


# ===========================================================================
# ProvenanceFactory.create()
# ===========================================================================


class TestProvenanceFactoryCreate:
    """Tests for ProvenanceFactory.create()."""

    def test_returns_data_provenance_instance(self) -> None:
        prov = ProvenanceFactory.create(
            instrument_id="NSE:NIFTY50:IDX",
            exchange="NSE",
            primary_provider=DataSource.ANGEL_ONE,
        )
        assert isinstance(prov, DataProvenance)

    def test_observation_id_is_uuid4(self) -> None:
        prov = ProvenanceFactory.create(
            instrument_id="NSE:NIFTY50:IDX",
            exchange="NSE",
            primary_provider=DataSource.ANGEL_ONE,
        )
        assert _is_uuid4(prov.dataObservationId)

    def test_received_at_ms_is_set(self) -> None:
        prov = ProvenanceFactory.create(
            instrument_id="NSE:NIFTY50:IDX",
            exchange="NSE",
            primary_provider=DataSource.ANGEL_ONE,
        )
        assert isinstance(prov.receivedAtMs, int)
        assert prov.receivedAtMs > 0

    def test_available_at_ms_is_set(self) -> None:
        prov = ProvenanceFactory.create(
            instrument_id="NSE:NIFTY50:IDX",
            exchange="NSE",
            primary_provider=DataSource.ANGEL_ONE,
        )
        assert isinstance(prov.availableAtMs, int)
        assert prov.availableAtMs > 0

    def test_instrument_id_propagated(self) -> None:
        prov = ProvenanceFactory.create(
            instrument_id="NSE:RELIANCE:EQ",
            exchange="NSE",
            primary_provider=DataSource.ANGEL_ONE,
        )
        assert prov.instrumentId == "NSE:RELIANCE:EQ"

    def test_exchange_propagated(self) -> None:
        prov = ProvenanceFactory.create(
            instrument_id="BTC:PERP",
            exchange="BINANCE",
            primary_provider=DataSource.BINANCE,
        )
        assert prov.exchange == "BINANCE"

    def test_source_set_from_enum(self) -> None:
        prov = ProvenanceFactory.create(
            instrument_id="NSE:NIFTY50:IDX",
            exchange="NSE",
            primary_provider=DataSource.UPSTOX,
        )
        assert prov.source == DataSource.UPSTOX

    def test_source_resolved_from_string(self) -> None:
        prov = ProvenanceFactory.create(
            instrument_id="NSE:NIFTY50:IDX",
            exchange="NSE",
            primary_provider="ANGEL_ONE",
        )
        assert prov.source == DataSource.ANGEL_ONE

    def test_unknown_string_provider_maps_to_unknown(self) -> None:
        prov = ProvenanceFactory.create(
            instrument_id="NSE:NIFTY50:IDX",
            exchange="NSE",
            primary_provider="NONEXISTENT_PROVIDER",
        )
        assert prov.source == DataSource.UNKNOWN

    def test_is_fallback_false_by_default(self) -> None:
        prov = ProvenanceFactory.create(
            instrument_id="NSE:NIFTY50:IDX",
            exchange="NSE",
            primary_provider=DataSource.ANGEL_ONE,
        )
        assert prov.isFallback is False

    def test_is_fallback_propagated(self) -> None:
        prov = ProvenanceFactory.create(
            instrument_id="NSE:NIFTY50:IDX",
            exchange="NSE",
            primary_provider=DataSource.OPENCHART,
            is_fallback=True,
            fallback_reason="primary_circuit_open",
        )
        assert prov.isFallback is True
        assert prov.fallbackReason == "primary_circuit_open"

    def test_default_normalisation_version(self) -> None:
        prov = ProvenanceFactory.create(
            instrument_id="NSE:NIFTY50:IDX",
            exchange="NSE",
            primary_provider=DataSource.ANGEL_ONE,
        )
        assert prov.normalisationVersion == ProvenanceFactory.DEFAULT_NORMALISATION_VERSION

    def test_custom_normalisation_version(self) -> None:
        prov = ProvenanceFactory.create(
            instrument_id="NSE:NIFTY50:IDX",
            exchange="NSE",
            primary_provider=DataSource.ANGEL_ONE,
            normalisation_version="3.0.0",
        )
        assert prov.normalisationVersion == "3.0.0"

    def test_interval_str_propagated(self) -> None:
        prov = ProvenanceFactory.create(
            instrument_id="NSE:NIFTY50:IDX",
            exchange="NSE",
            primary_provider=DataSource.ANGEL_ONE,
            interval_str="5m",
        )
        assert prov.intervalStr == "5m"

    def test_source_type_propagated(self) -> None:
        prov = ProvenanceFactory.create(
            instrument_id="NSE:NIFTY50:IDX",
            exchange="NSE",
            primary_provider=DataSource.ANGEL_ONE,
            source_type=ProviderSourceType.BROKER_AUTHENTICATED,
        )
        assert prov.sourceType == ProviderSourceType.BROKER_AUTHENTICATED

    def test_dataset_version_propagated(self) -> None:
        prov = ProvenanceFactory.create(
            instrument_id="NSE:NIFTY50:IDX",
            exchange="NSE",
            primary_provider=DataSource.ANGEL_ONE,
            dataset_version=42,
        )
        assert prov.datasetVersion == 42

    def test_row_count_propagated(self) -> None:
        prov = ProvenanceFactory.create(
            instrument_id="NSE:NIFTY50:IDX",
            exchange="NSE",
            primary_provider=DataSource.ANGEL_ONE,
            row_count=375,
        )
        assert prov.rowCount == 375

    def test_response_hash_none_when_no_payload(self) -> None:
        prov = ProvenanceFactory.create(
            instrument_id="NSE:NIFTY50:IDX",
            exchange="NSE",
            primary_provider=DataSource.ANGEL_ONE,
        )
        assert prov.responseHash is None

    def test_response_hash_from_bytes_payload(self) -> None:
        raw = b'{"ltp": 22150.50}'
        expected = _sha256(raw)
        prov = ProvenanceFactory.create(
            instrument_id="NSE:NIFTY50:IDX",
            exchange="NSE",
            primary_provider=DataSource.ANGEL_ONE,
            source_payload=raw,
        )
        assert prov.responseHash == expected

    def test_response_hash_from_str_payload(self) -> None:
        raw = '{"ltp": 22150.50}'
        expected = _sha256(raw)
        prov = ProvenanceFactory.create(
            instrument_id="NSE:NIFTY50:IDX",
            exchange="NSE",
            primary_provider=DataSource.ANGEL_ONE,
            source_payload=raw,
        )
        assert prov.responseHash == expected

    def test_two_calls_produce_distinct_observation_ids(self) -> None:
        prov_a = ProvenanceFactory.create(
            instrument_id="NSE:NIFTY50:IDX",
            exchange="NSE",
            primary_provider=DataSource.ANGEL_ONE,
        )
        prov_b = ProvenanceFactory.create(
            instrument_id="NSE:NIFTY50:IDX",
            exchange="NSE",
            primary_provider=DataSource.ANGEL_ONE,
        )
        assert prov_a.dataObservationId != prov_b.dataObservationId

    def test_100_calls_produce_unique_observation_ids(self) -> None:
        """100 successive create() calls must all produce distinct observation IDs."""
        ids = {
            ProvenanceFactory.create(
                instrument_id="NSE:NIFTY50:IDX",
                exchange="NSE",
                primary_provider=DataSource.ANGEL_ONE,
            ).dataObservationId
            for _ in range(100)
        }
        assert len(ids) == 100, "Duplicate dataObservationId detected across 100 calls"

    def test_validation_applied_is_true(self) -> None:
        prov = ProvenanceFactory.create(
            instrument_id="NSE:NIFTY50:IDX",
            exchange="NSE",
            primary_provider=DataSource.ANGEL_ONE,
        )
        assert prov.validationApplied is True

    def test_data_trust_status_is_trusted(self) -> None:
        prov = ProvenanceFactory.create(
            instrument_id="NSE:NIFTY50:IDX",
            exchange="NSE",
            primary_provider=DataSource.ANGEL_ONE,
        )
        assert prov.dataTrustStatus == DataTrustStatus.TRUSTED

    def test_source_chain_contains_provider_name(self) -> None:
        prov = ProvenanceFactory.create(
            instrument_id="NSE:NIFTY50:IDX",
            exchange="NSE",
            primary_provider=DataSource.ANGEL_ONE,
        )
        assert len(prov.sourceChain) == 1
        assert prov.sourceChain[0] == DataSource.ANGEL_ONE.value

    def test_event_time_ms_propagated(self) -> None:
        prov = ProvenanceFactory.create(
            instrument_id="NSE:NIFTY50:IDX",
            exchange="NSE",
            primary_provider=DataSource.ANGEL_ONE,
            event_time_ms=1705300000123,
        )
        assert prov.eventTimeMs == 1705300000123


# ===========================================================================
# ProvenanceFactory.from_tick()
# ===========================================================================


class TestProvenanceFactoryFromTick:
    """Tests for ProvenanceFactory.from_tick()."""

    def _minimal_tick(self) -> dict[str, Any]:
        return {
            "instrumentId": "NSE:BANKNIFTY:IDX",
            "exchange": "NSE",
            "source": "ANGEL_ONE",
            "ltp": 48500.0,
            "volume": 12345,
        }

    def test_returns_data_provenance_instance(self) -> None:
        prov = ProvenanceFactory.from_tick(self._minimal_tick())
        assert isinstance(prov, DataProvenance)

    def test_instrument_id_extracted(self) -> None:
        prov = ProvenanceFactory.from_tick(self._minimal_tick())
        assert prov.instrumentId == "NSE:BANKNIFTY:IDX"

    def test_exchange_extracted(self) -> None:
        prov = ProvenanceFactory.from_tick(self._minimal_tick())
        assert prov.exchange == "NSE"

    def test_source_extracted_from_source_key(self) -> None:
        prov = ProvenanceFactory.from_tick(self._minimal_tick())
        assert prov.source == DataSource.ANGEL_ONE

    def test_observation_id_is_uuid4(self) -> None:
        prov = ProvenanceFactory.from_tick(self._minimal_tick())
        assert _is_uuid4(prov.dataObservationId)

    def test_received_at_ms_is_positive_int(self) -> None:
        prov = ProvenanceFactory.from_tick(self._minimal_tick())
        assert isinstance(prov.receivedAtMs, int)
        assert prov.receivedAtMs > 0

    def test_event_time_ms_extracted_when_present(self) -> None:
        tick = {**self._minimal_tick(), "eventTimeMs": 1705300000999}
        prov = ProvenanceFactory.from_tick(tick)
        assert prov.eventTimeMs == 1705300000999

    def test_event_time_ms_none_when_absent(self) -> None:
        prov = ProvenanceFactory.from_tick(self._minimal_tick())
        assert prov.eventTimeMs is None

    def test_raises_key_error_without_instrument_id(self) -> None:
        tick = {"exchange": "NSE", "source": "ANGEL_ONE"}
        with pytest.raises(KeyError):
            ProvenanceFactory.from_tick(tick)

    def test_raises_key_error_without_exchange(self) -> None:
        tick = {"instrumentId": "NSE:NIFTY50:IDX", "source": "ANGEL_ONE"}
        with pytest.raises(KeyError):
            ProvenanceFactory.from_tick(tick)

    def test_raises_key_error_without_source(self) -> None:
        tick = {"instrumentId": "NSE:NIFTY50:IDX", "exchange": "NSE"}
        with pytest.raises(KeyError):
            ProvenanceFactory.from_tick(tick)

    def test_response_hash_set_when_payload_provided(self) -> None:
        raw = b'{"ltp": 48500.0}'
        expected = _sha256(raw)
        prov = ProvenanceFactory.from_tick(self._minimal_tick(), source_payload=raw)
        assert prov.responseHash == expected

    def test_response_hash_none_by_default(self) -> None:
        prov = ProvenanceFactory.from_tick(self._minimal_tick())
        assert prov.responseHash is None

    def test_custom_normalisation_version(self) -> None:
        prov = ProvenanceFactory.from_tick(
            self._minimal_tick(),
            normalisation_version="3.1.0",
        )
        assert prov.normalisationVersion == "3.1.0"

    def test_two_calls_produce_distinct_observation_ids(self) -> None:
        tick = self._minimal_tick()
        prov_a = ProvenanceFactory.from_tick(tick)
        prov_b = ProvenanceFactory.from_tick(tick)
        assert prov_a.dataObservationId != prov_b.dataObservationId

    def test_multiple_exchanges_produce_correct_field(self) -> None:
        for exchange in ("NSE", "NFO", "BINANCE", "DERIBIT"):
            tick = {
                "instrumentId": f"{exchange}:INSTRUMENT",
                "exchange": exchange,
                "source": "BINANCE",
            }
            prov = ProvenanceFactory.from_tick(tick)
            assert prov.exchange == exchange
