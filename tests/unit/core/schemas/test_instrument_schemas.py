"""
tests/unit/core/schemas/test_instrument_schemas.py

Unit tests for src/core/schemas/instrument.py.

Covers:
- All enum values accepted and rejected
- Instrument model field defaults
- Provider tokens excluded from default serialisation
- Provider tokens included when with_provider_tokens() is called
- FnoUniverseSnapshot validation
- InstrumentLifecycleEvent validation
- Option type constraint (only CE/PE allowed)
- PROVIDER_TOKEN_FIELDS constant

Requirements: 2.2, 2.7, 11.4
"""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from src.core.schemas.instrument import (
    ChangeType,
    ExchangeEnum,
    FnoUniverseSnapshot,
    Instrument,
    InstrumentLifecycleEvent,
    InstrumentType,
    PROVIDER_TOKEN_FIELDS,
    SegmentEnum,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_instrument(**overrides) -> Instrument:
    """Build a minimal valid Instrument with sensible defaults."""
    defaults: dict = {
        "instrumentId": "NSE:RELIANCE:EQ",
        "tradingSymbol": "RELIANCE",
        "exchange": ExchangeEnum.NSE,
        "segment": SegmentEnum.EQ,
        "instrumentType": InstrumentType.EQ,
        "activeFrom": date(2020, 1, 1),
    }
    defaults.update(overrides)
    return Instrument(**defaults)


def make_fno_snapshot(**overrides) -> FnoUniverseSnapshot:
    """Build a minimal valid FnoUniverseSnapshot."""
    defaults: dict = {
        "snapshotVersion": 1,
        "checksum": "a" * 64,
        "generatedAt": "2025-01-15T08:45:00.000Z",
        "effectiveFrom": date(2025, 1, 15),
        "fnoEquityCount": 180,
        "fnoIndexCount": 5,
        "constituentCount": 185,
    }
    defaults.update(overrides)
    return FnoUniverseSnapshot(**defaults)


def make_lifecycle_event(**overrides) -> InstrumentLifecycleEvent:
    """Build a minimal valid InstrumentLifecycleEvent."""
    defaults: dict = {
        "symbol": "RELIANCE",
        "changeType": ChangeType.ADDED,
        "effectiveDate": date(2025, 1, 15),
        "source": "NSE_CIRCULAR",
        "snapshotVersion": 2,
    }
    defaults.update(overrides)
    return InstrumentLifecycleEvent(**defaults)


# ---------------------------------------------------------------------------
# InstrumentType enum
# ---------------------------------------------------------------------------


class TestInstrumentTypeEnum:
    def test_all_values_accepted(self):
        for val in ("EQ", "FUTIDX", "FUTSTK", "OPTIDX", "OPTSTK", "ETF", "IDX"):
            assert InstrumentType(val).value == val

    def test_invalid_value_raises(self):
        with pytest.raises(ValueError):
            InstrumentType("INVALID")

    def test_str_behaviour(self):
        # str(Enum) in Python 3.11+ includes the class name; use .value for the raw string
        assert InstrumentType.EQ == "EQ"
        assert InstrumentType.FUTIDX.value == "FUTIDX"


# ---------------------------------------------------------------------------
# ExchangeEnum
# ---------------------------------------------------------------------------


class TestExchangeEnum:
    def test_all_values_accepted(self):
        for val in ("NSE", "NFO", "BSE", "BFO", "MCX"):
            assert ExchangeEnum(val).value == val

    def test_invalid_value_raises(self):
        with pytest.raises(ValueError):
            ExchangeEnum("CRYPTO")

    def test_str_behaviour(self):
        assert ExchangeEnum.NSE == "NSE"


# ---------------------------------------------------------------------------
# SegmentEnum
# ---------------------------------------------------------------------------


class TestSegmentEnum:
    def test_all_values_accepted(self):
        for val in ("EQ", "FO", "CD", "COM", "CDS"):
            assert SegmentEnum(val).value == val

    def test_invalid_value_raises(self):
        with pytest.raises(ValueError):
            SegmentEnum("UNKNOWN_SEG")

    def test_str_behaviour(self):
        assert SegmentEnum.FO == "FO"


# ---------------------------------------------------------------------------
# ChangeType enum
# ---------------------------------------------------------------------------


class TestChangeTypeEnum:
    def test_all_values_accepted(self):
        for val in ("ADDED", "REMOVED", "SUSPENDED"):
            assert ChangeType(val).value == val

    def test_invalid_value_raises(self):
        with pytest.raises(ValueError):
            ChangeType("MODIFIED")

    def test_str_behaviour(self):
        assert ChangeType.ADDED == "ADDED"


# ---------------------------------------------------------------------------
# Instrument model — field defaults
# ---------------------------------------------------------------------------


class TestInstrumentDefaults:
    def test_minimal_valid_instrument(self):
        inst = make_instrument()
        assert inst.instrumentId == "NSE:RELIANCE:EQ"
        assert inst.tradingSymbol == "RELIANCE"

    def test_lot_size_default(self):
        inst = make_instrument()
        assert inst.lotSize == 1

    def test_tick_size_default(self):
        inst = make_instrument()
        assert inst.tickSize == 0.05

    def test_active_to_default_none(self):
        inst = make_instrument()
        assert inst.activeTo is None

    def test_display_symbol_default_none(self):
        inst = make_instrument()
        assert inst.displaySymbol is None

    def test_isin_default_none(self):
        inst = make_instrument()
        assert inst.isin is None

    def test_underlying_default_none(self):
        inst = make_instrument()
        assert inst.underlying is None

    def test_expiry_default_none(self):
        inst = make_instrument()
        assert inst.expiry is None

    def test_strike_default_none(self):
        inst = make_instrument()
        assert inst.strike is None

    def test_option_type_default_none(self):
        inst = make_instrument()
        assert inst.optionType is None

    def test_provider_token_defaults_none(self):
        inst = make_instrument()
        assert inst.angelToken is None
        assert inst.angelSymbol is None
        assert inst.upstoxKey is None
        assert inst.upstoxSymbol is None

    def test_exchange_accepts_enum_value(self):
        inst = make_instrument(exchange=ExchangeEnum.NFO)
        assert inst.exchange == ExchangeEnum.NFO

    def test_exchange_accepts_string_value(self):
        inst = make_instrument(exchange="BSE")
        assert inst.exchange == ExchangeEnum.BSE

    def test_lot_size_must_be_positive(self):
        with pytest.raises(ValidationError):
            make_instrument(lotSize=0)

    def test_tick_size_must_be_positive(self):
        with pytest.raises(ValidationError):
            make_instrument(tickSize=0.0)

    def test_full_derivative_instrument(self):
        inst = make_instrument(
            instrumentId="NFO:NIFTY25JANFUT:FUTIDX",
            tradingSymbol="NIFTY25JANFUT",
            exchange=ExchangeEnum.NFO,
            segment=SegmentEnum.FO,
            instrumentType=InstrumentType.FUTIDX,
            underlying="NIFTY",
            expiry=date(2025, 1, 30),
            lotSize=50,
            tickSize=0.05,
        )
        assert inst.underlying == "NIFTY"
        assert inst.expiry == date(2025, 1, 30)
        assert inst.lotSize == 50


# ---------------------------------------------------------------------------
# Provider tokens — excluded from default serialisation
# ---------------------------------------------------------------------------


class TestProviderTokenExclusion:
    def test_tokens_excluded_from_model_dump(self):
        inst = make_instrument(
            angelToken="12345",
            angelSymbol="RELIANCE-EQ",
            upstoxKey="NSE_EQ|RELIANCE",
            upstoxSymbol="RELIANCE",
        )
        dumped = inst.model_dump()
        assert "angelToken" not in dumped
        assert "angelSymbol" not in dumped
        assert "upstoxKey" not in dumped
        assert "upstoxSymbol" not in dumped

    def test_tokens_excluded_from_model_dump_json(self):
        inst = make_instrument(
            angelToken="SECRET",
            upstoxKey="ALSO_SECRET",
        )
        json_str = inst.model_dump_json()
        assert "angelToken" not in json_str
        assert "SECRET" not in json_str
        assert "upstoxKey" not in json_str
        assert "ALSO_SECRET" not in json_str

    def test_non_token_fields_present_in_dump(self):
        inst = make_instrument(
            angelToken="t1",
            tradingSymbol="RELIANCE",
        )
        dumped = inst.model_dump()
        assert "tradingSymbol" in dumped
        assert dumped["tradingSymbol"] == "RELIANCE"
        assert "instrumentId" in dumped

    def test_tokens_still_accessible_as_attributes(self):
        """Even though excluded from serialisation, the values are on the object."""
        inst = make_instrument(
            angelToken="ANGEL_TOK",
            angelSymbol="ANGEL_SYM",
            upstoxKey="UPSTOX_KEY",
            upstoxSymbol="UPSTOX_SYM",
        )
        assert inst.angelToken == "ANGEL_TOK"
        assert inst.angelSymbol == "ANGEL_SYM"
        assert inst.upstoxKey == "UPSTOX_KEY"
        assert inst.upstoxSymbol == "UPSTOX_SYM"

    def test_provider_token_fields_constant(self):
        """PROVIDER_TOKEN_FIELDS lists all four token field names."""
        assert PROVIDER_TOKEN_FIELDS == {"angelToken", "angelSymbol", "upstoxKey", "upstoxSymbol"}


# ---------------------------------------------------------------------------
# with_provider_tokens() — includes tokens in output
# ---------------------------------------------------------------------------


class TestWithProviderTokens:
    def test_returns_dict(self):
        inst = make_instrument()
        result = inst.with_provider_tokens()
        assert isinstance(result, dict)

    def test_token_fields_present_in_output(self):
        inst = make_instrument(
            angelToken="ANGEL_T",
            angelSymbol="ANGEL_S",
            upstoxKey="U_KEY",
            upstoxSymbol="U_SYM",
        )
        result = inst.with_provider_tokens()
        assert result["angelToken"] == "ANGEL_T"
        assert result["angelSymbol"] == "ANGEL_S"
        assert result["upstoxKey"] == "U_KEY"
        assert result["upstoxSymbol"] == "U_SYM"

    def test_token_fields_present_even_when_none(self):
        inst = make_instrument()
        result = inst.with_provider_tokens()
        assert "angelToken" in result
        assert result["angelToken"] is None
        assert "upstoxKey" in result
        assert result["upstoxKey"] is None

    def test_non_token_fields_still_present(self):
        inst = make_instrument(tradingSymbol="INFY", angelToken="T")
        result = inst.with_provider_tokens()
        assert result["tradingSymbol"] == "INFY"
        assert result["instrumentId"] == "NSE:RELIANCE:EQ"

    def test_does_not_mutate_model_dump(self):
        """Calling with_provider_tokens() should not affect model_dump()."""
        inst = make_instrument(angelToken="SECRET")
        _ = inst.with_provider_tokens()
        dumped = inst.model_dump()
        assert "angelToken" not in dumped


# ---------------------------------------------------------------------------
# Instrument optionType constraint
# ---------------------------------------------------------------------------


class TestOptionTypeConstraint:
    def test_ce_accepted(self):
        inst = make_instrument(
            instrumentType=InstrumentType.OPTIDX,
            optionType="CE",
            strike=22000.0,
            expiry=date(2025, 1, 30),
        )
        assert inst.optionType == "CE"

    def test_pe_accepted(self):
        inst = make_instrument(
            instrumentType=InstrumentType.OPTSTK,
            optionType="PE",
            strike=500.0,
            expiry=date(2025, 1, 30),
        )
        assert inst.optionType == "PE"

    def test_none_accepted(self):
        inst = make_instrument(optionType=None)
        assert inst.optionType is None

    def test_invalid_option_type_raises(self):
        with pytest.raises(ValidationError):
            make_instrument(optionType="CALL")

    def test_lowercase_ce_raises(self):
        with pytest.raises(ValidationError):
            make_instrument(optionType="ce")

    def test_call_string_raises(self):
        with pytest.raises(ValidationError):
            make_instrument(optionType="CALL")

    def test_put_string_raises(self):
        with pytest.raises(ValidationError):
            make_instrument(optionType="PUT")


# ---------------------------------------------------------------------------
# FnoUniverseSnapshot
# ---------------------------------------------------------------------------


class TestFnoUniverseSnapshot:
    def test_minimal_valid_snapshot(self):
        snap = make_fno_snapshot()
        assert snap.snapshotVersion == 1
        assert snap.status == "ACTIVE"
        assert snap.effectiveTo is None

    def test_status_default_active(self):
        snap = make_fno_snapshot()
        assert snap.status == "ACTIVE"

    def test_status_superseded_accepted(self):
        snap = make_fno_snapshot(status="SUPERSEDED")
        assert snap.status == "SUPERSEDED"

    def test_invalid_status_raises(self):
        with pytest.raises(ValidationError):
            make_fno_snapshot(status="EXPIRED")

    def test_constituent_count_zero_accepted(self):
        snap = make_fno_snapshot(constituentCount=0, fnoEquityCount=0, fnoIndexCount=0)
        assert snap.constituentCount == 0

    def test_negative_counts_rejected(self):
        with pytest.raises(ValidationError):
            make_fno_snapshot(fnoEquityCount=-1)

    def test_effective_to_can_be_set(self):
        snap = make_fno_snapshot(
            effectiveTo=date(2025, 1, 16),
            status="SUPERSEDED",
        )
        assert snap.effectiveTo == date(2025, 1, 16)

    def test_snapshot_version_must_be_positive(self):
        with pytest.raises(ValidationError):
            make_fno_snapshot(snapshotVersion=0)

    def test_checksum_field_stored(self):
        checksum = "b" * 64
        snap = make_fno_snapshot(checksum=checksum)
        assert snap.checksum == checksum

    def test_generated_at_stored_as_string(self):
        snap = make_fno_snapshot(generatedAt="2025-01-15T08:45:00.000Z")
        assert snap.generatedAt == "2025-01-15T08:45:00.000Z"

    def test_model_dump_contains_all_fields(self):
        snap = make_fno_snapshot()
        d = snap.model_dump()
        for key in (
            "snapshotVersion", "checksum", "generatedAt", "effectiveFrom",
            "effectiveTo", "fnoEquityCount", "fnoIndexCount", "constituentCount", "status",
        ):
            assert key in d


# ---------------------------------------------------------------------------
# InstrumentLifecycleEvent
# ---------------------------------------------------------------------------


class TestInstrumentLifecycleEvent:
    def test_added_event(self):
        evt = make_lifecycle_event(changeType=ChangeType.ADDED)
        assert evt.changeType == ChangeType.ADDED

    def test_removed_event(self):
        evt = make_lifecycle_event(changeType=ChangeType.REMOVED)
        assert evt.changeType == ChangeType.REMOVED

    def test_suspended_event(self):
        evt = make_lifecycle_event(changeType=ChangeType.SUSPENDED)
        assert evt.changeType == ChangeType.SUSPENDED

    def test_all_fields_stored(self):
        evt = make_lifecycle_event(
            symbol="TATAMOTORS",
            changeType=ChangeType.ADDED,
            effectiveDate=date(2025, 3, 1),
            source="NSE_NOTIFICATION",
            snapshotVersion=5,
        )
        assert evt.symbol == "TATAMOTORS"
        assert evt.effectiveDate == date(2025, 3, 1)
        assert evt.source == "NSE_NOTIFICATION"
        assert evt.snapshotVersion == 5

    def test_invalid_change_type_raises(self):
        with pytest.raises(ValidationError):
            make_lifecycle_event(changeType="MODIFIED")

    def test_snapshot_version_must_be_positive(self):
        with pytest.raises(ValidationError):
            make_lifecycle_event(snapshotVersion=0)

    def test_model_dump_contains_all_fields(self):
        evt = make_lifecycle_event()
        d = evt.model_dump()
        for key in ("symbol", "changeType", "effectiveDate", "source", "snapshotVersion"):
            assert key in d

    def test_change_type_serialised_as_string(self):
        evt = make_lifecycle_event(changeType=ChangeType.REMOVED)
        d = evt.model_dump()
        assert d["changeType"] == "REMOVED"


# ---------------------------------------------------------------------------
# Instrument — required field validation
# ---------------------------------------------------------------------------


class TestInstrumentRequiredFields:
    def test_missing_instrument_id_raises(self):
        with pytest.raises(ValidationError):
            Instrument(
                tradingSymbol="RELIANCE",
                exchange=ExchangeEnum.NSE,
                segment=SegmentEnum.EQ,
                instrumentType=InstrumentType.EQ,
                activeFrom=date(2020, 1, 1),
            )

    def test_missing_trading_symbol_raises(self):
        with pytest.raises(ValidationError):
            Instrument(
                instrumentId="NSE:RELIANCE:EQ",
                exchange=ExchangeEnum.NSE,
                segment=SegmentEnum.EQ,
                instrumentType=InstrumentType.EQ,
                activeFrom=date(2020, 1, 1),
            )

    def test_missing_exchange_raises(self):
        with pytest.raises(ValidationError):
            Instrument(
                instrumentId="NSE:RELIANCE:EQ",
                tradingSymbol="RELIANCE",
                segment=SegmentEnum.EQ,
                instrumentType=InstrumentType.EQ,
                activeFrom=date(2020, 1, 1),
            )

    def test_missing_active_from_raises(self):
        with pytest.raises(ValidationError):
            Instrument(
                instrumentId="NSE:RELIANCE:EQ",
                tradingSymbol="RELIANCE",
                exchange=ExchangeEnum.NSE,
                segment=SegmentEnum.EQ,
                instrumentType=InstrumentType.EQ,
            )

    def test_invalid_exchange_raises(self):
        with pytest.raises(ValidationError):
            make_instrument(exchange="INVALID_EXCHANGE")

    def test_invalid_segment_raises(self):
        with pytest.raises(ValidationError):
            make_instrument(segment="UNKNOWN")

    def test_invalid_instrument_type_raises(self):
        with pytest.raises(ValidationError):
            make_instrument(instrumentType="CRYPTO")
