"""
tests/unit/engines/test_instrument_master.py

Unit tests for src/engines/instrument_master.py — InstrumentMasterService.

Covers:
- get_instrument: cache hit, cache miss
- search_instruments: individual filters (exchange, instrument_type, underlying,
  segment, expiry), combined filters, active_only flag
- Point-in-time filtering: active, expired, and future-listed instruments
- resolve_provider_tokens: angel_one, upstox, unknown provider, missing instrument
- load_from_db: populates in-memory store, replaces previous data, skips bad rows
- is_loaded / __len__ helpers

Requirements: 2.3, 2.7, 2.8
"""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.schemas.instrument import (
    ExchangeEnum,
    Instrument,
    InstrumentType,
    SegmentEnum,
)
from src.engines.instrument_master import InstrumentMasterService, _row_to_instrument


# ---------------------------------------------------------------------------
# Shared fixtures / builders
# ---------------------------------------------------------------------------


def _make_instrument(
    instrument_id: str = "NSE:RELIANCE:EQ",
    trading_symbol: str = "RELIANCE",
    exchange: ExchangeEnum = ExchangeEnum.NSE,
    segment: SegmentEnum = SegmentEnum.EQ,
    instrument_type: InstrumentType = InstrumentType.EQ,
    underlying: str | None = None,
    expiry: date | None = None,
    option_type: str | None = None,
    strike: float | None = None,
    active_from: date = date(2020, 1, 1),
    active_to: date | None = None,
    angel_token: str | None = None,
    angel_symbol: str | None = None,
    upstox_key: str | None = None,
    upstox_symbol: str | None = None,
    lot_size: int = 1,
    tick_size: float = 0.05,
) -> Instrument:
    """Build a valid Instrument with sensible defaults for test use."""
    return Instrument(
        instrumentId=instrument_id,
        tradingSymbol=trading_symbol,
        exchange=exchange,
        segment=segment,
        instrumentType=instrument_type,
        underlying=underlying,
        expiry=expiry,
        optionType=option_type,
        strike=strike,
        activeFrom=active_from,
        activeTo=active_to,
        angelToken=angel_token,
        angelSymbol=angel_symbol,
        upstoxKey=upstox_key,
        upstoxSymbol=upstox_symbol,
        lotSize=lot_size,
        tickSize=tick_size,
    )


def _make_service(*instruments: Instrument) -> InstrumentMasterService:
    """Return a pre-loaded InstrumentMasterService without hitting the DB."""
    svc = InstrumentMasterService()
    svc._instruments = {inst.instrumentId: inst for inst in instruments}
    return svc


# ---------------------------------------------------------------------------
# _row_to_instrument helper
# ---------------------------------------------------------------------------


class TestRowToInstrument:
    """Tests for the internal _row_to_instrument mapping function."""

    def test_basic_equity_row(self):
        row = {
            "instrument_id": "NSE:TCS:EQ",
            "trading_symbol": "TCS",
            "display_symbol": None,
            "isin": None,
            "exchange": "NSE",
            "segment": "EQ",
            "instrument_type": "EQ",
            "underlying": None,
            "expiry": None,
            "strike": None,
            "option_type": None,
            "lot_size": 1,
            "tick_size": 0.05,
            "active_from": date(2010, 1, 1),
            "active_to": None,
            "angel_token": "1594",
            "angel_symbol": "TCS-EQ",
            "upstox_key": "NSE_EQ|TCS",
            "upstox_symbol": "TCS",
        }
        inst = _row_to_instrument(row)
        assert inst.instrumentId == "NSE:TCS:EQ"
        assert inst.tradingSymbol == "TCS"
        assert inst.exchange == ExchangeEnum.NSE
        assert inst.segment == SegmentEnum.EQ
        assert inst.instrumentType == InstrumentType.EQ
        assert inst.activeFrom == date(2010, 1, 1)
        assert inst.activeTo is None

    def test_provider_tokens_mapped(self):
        row = {
            "instrument_id": "NSE:INFY:EQ",
            "trading_symbol": "INFY",
            "exchange": "NSE",
            "segment": "EQ",
            "instrument_type": "EQ",
            "lot_size": 1,
            "tick_size": 0.05,
            "active_from": date(2015, 1, 1),
            "active_to": None,
            "angel_token": "1594",
            "angel_symbol": "INFY-EQ",
            "upstox_key": "NSE_EQ|INFY",
            "upstox_symbol": "INFY",
        }
        inst = _row_to_instrument(row)
        assert inst.angelToken == "1594"
        assert inst.angelSymbol == "INFY-EQ"
        assert inst.upstoxKey == "NSE_EQ|INFY"
        assert inst.upstoxSymbol == "INFY"

    def test_provider_tokens_excluded_from_dump(self):
        row = {
            "instrument_id": "NSE:INFY:EQ",
            "trading_symbol": "INFY",
            "exchange": "NSE",
            "segment": "EQ",
            "instrument_type": "EQ",
            "lot_size": 1,
            "tick_size": 0.05,
            "active_from": date(2015, 1, 1),
            "active_to": None,
            "angel_token": "1594",
            "angel_symbol": "INFY-EQ",
            "upstox_key": "NSE_EQ|INFY",
            "upstox_symbol": "INFY",
        }
        inst = _row_to_instrument(row)
        dumped = inst.model_dump()
        assert "angelToken" not in dumped
        assert "upstoxKey" not in dumped

    def test_derivative_row(self):
        row = {
            "instrument_id": "NFO:NIFTY25JANFUT:FUTIDX",
            "trading_symbol": "NIFTY25JANFUT",
            "display_symbol": "NIFTY Jan 2025 Fut",
            "exchange": "NFO",
            "segment": "FO",
            "instrument_type": "FUTIDX",
            "underlying": "NIFTY",
            "expiry": date(2025, 1, 30),
            "strike": None,
            "option_type": None,
            "lot_size": 50,
            "tick_size": 0.05,
            "active_from": date(2024, 11, 1),
            "active_to": None,
            "isin": None,
            "angel_token": None,
            "angel_symbol": None,
            "upstox_key": None,
            "upstox_symbol": None,
        }
        inst = _row_to_instrument(row)
        assert inst.underlying == "NIFTY"
        assert inst.expiry == date(2025, 1, 30)
        assert inst.lotSize == 50
        assert inst.instrumentType == InstrumentType.FUTIDX

    def test_default_lot_size_when_missing(self):
        row = {
            "instrument_id": "NSE:X:EQ",
            "trading_symbol": "X",
            "exchange": "NSE",
            "segment": "EQ",
            "instrument_type": "EQ",
            "active_from": date(2020, 1, 1),
            # lot_size and tick_size absent from row
        }
        inst = _row_to_instrument(row)
        assert inst.lotSize == 1
        assert inst.tickSize == 0.05


# ---------------------------------------------------------------------------
# InstrumentMasterService.get_instrument
# ---------------------------------------------------------------------------


class TestGetInstrument:
    def test_returns_instrument_on_hit(self):
        inst = _make_instrument()
        svc = _make_service(inst)
        result = svc.get_instrument("NSE:RELIANCE:EQ")
        assert result is inst

    def test_returns_none_on_miss(self):
        svc = _make_service()
        result = svc.get_instrument("NSE:UNKNOWN:EQ")
        assert result is None

    def test_case_sensitive_lookup(self):
        inst = _make_instrument(instrument_id="NSE:RELIANCE:EQ")
        svc = _make_service(inst)
        # ID must match exactly — lowercase should miss
        assert svc.get_instrument("nse:reliance:eq") is None
        assert svc.get_instrument("NSE:RELIANCE:EQ") is inst

    def test_returns_expired_instrument(self):
        """get_instrument returns ALL instruments regardless of active status."""
        expired = _make_instrument(
            instrument_id="NSE:OLD:EQ",
            active_from=date(2015, 1, 1),
            active_to=date(2022, 1, 1),
        )
        svc = _make_service(expired)
        result = svc.get_instrument("NSE:OLD:EQ")
        assert result is expired

    def test_provider_tokens_accessible_on_returned_instrument(self):
        inst = _make_instrument(angel_token="SECRET_TOKEN", upstox_key="UPSTOX_KEY")
        svc = _make_service(inst)
        result = svc.get_instrument("NSE:RELIANCE:EQ")
        assert result is not None
        assert result.angelToken == "SECRET_TOKEN"
        assert result.upstoxKey == "UPSTOX_KEY"


# ---------------------------------------------------------------------------
# InstrumentMasterService.search_instruments — active filter
# ---------------------------------------------------------------------------


class TestSearchInstrumentsActiveFilter:
    def _make_svc(self) -> tuple[InstrumentMasterService, Instrument, Instrument, Instrument]:
        active = _make_instrument(
            instrument_id="NSE:ACTIVE:EQ",
            active_from=date(2020, 1, 1),
            active_to=None,
        )
        expired = _make_instrument(
            instrument_id="NSE:EXPIRED:EQ",
            trading_symbol="EXPIRED",
            active_from=date(2015, 1, 1),
            active_to=date(2023, 1, 1),  # expired in the past
        )
        future_listed = _make_instrument(
            instrument_id="NSE:FUTURE:EQ",
            trading_symbol="FUTURE",
            active_from=date(2099, 1, 1),  # not yet listed
            active_to=None,
        )
        return _make_service(active, expired, future_listed), active, expired, future_listed

    def test_active_only_excludes_expired(self):
        svc, active, expired, _ = self._make_svc()
        results = svc.search_instruments(active_only=True)
        ids = {r.instrumentId for r in results}
        assert "NSE:ACTIVE:EQ" in ids
        assert "NSE:EXPIRED:EQ" not in ids

    def test_active_only_excludes_future_listed(self):
        svc, active, _, future_listed = self._make_svc()
        results = svc.search_instruments(active_only=True)
        ids = {r.instrumentId for r in results}
        assert "NSE:FUTURE:EQ" not in ids

    def test_active_only_false_includes_expired(self):
        svc, active, expired, future_listed = self._make_svc()
        results = svc.search_instruments(active_only=False)
        ids = {r.instrumentId for r in results}
        assert "NSE:ACTIVE:EQ" in ids
        assert "NSE:EXPIRED:EQ" in ids
        assert "NSE:FUTURE:EQ" in ids

    def test_point_in_time_as_of_yesterday(self):
        """active_to today means it was active yesterday but not today."""
        today = date.today()
        from datetime import timedelta
        yesterday = today - timedelta(days=1)
        yesterday_expired = _make_instrument(
            instrument_id="NSE:YEST:EQ",
            trading_symbol="YEST",
            active_from=date(2020, 1, 1),
            active_to=yesterday,  # expired yesterday
        )
        svc = _make_service(yesterday_expired)
        # active_only=True relative to today should exclude it
        results_today = svc.search_instruments(active_only=True)
        assert len(results_today) == 0
        # but it was active yesterday
        results_yesterday = svc.search_instruments(active_only=True, as_of=yesterday)
        assert len(results_yesterday) == 1

    def test_active_on_boundary_active_from_today(self):
        """An instrument that became active today should be returned."""
        today = date.today()
        just_listed = _make_instrument(
            instrument_id="NSE:NEW:EQ",
            trading_symbol="NEW",
            active_from=today,
            active_to=None,
        )
        svc = _make_service(just_listed)
        results = svc.search_instruments(active_only=True)
        assert len(results) == 1
        assert results[0].instrumentId == "NSE:NEW:EQ"

    def test_active_on_boundary_active_to_today(self):
        """An instrument expiring today (activeTo == today) is still active today."""
        today = date.today()
        expiring_today = _make_instrument(
            instrument_id="NSE:EXP_TODAY:EQ",
            trading_symbol="EXP_TODAY",
            active_from=date(2020, 1, 1),
            active_to=today,
        )
        svc = _make_service(expiring_today)
        results = svc.search_instruments(active_only=True)
        # activeTo >= today, so it should be included
        assert len(results) == 1

    def test_empty_store_returns_empty_list(self):
        svc = InstrumentMasterService()
        assert svc.search_instruments() == []


# ---------------------------------------------------------------------------
# InstrumentMasterService.search_instruments — field filters
# ---------------------------------------------------------------------------


class TestSearchInstrumentsFilters:
    def _build_universe(self) -> InstrumentMasterService:
        instruments = [
            _make_instrument(
                instrument_id="NSE:RELIANCE:EQ",
                trading_symbol="RELIANCE",
                exchange=ExchangeEnum.NSE,
                segment=SegmentEnum.EQ,
                instrument_type=InstrumentType.EQ,
            ),
            _make_instrument(
                instrument_id="NSE:TCS:EQ",
                trading_symbol="TCS",
                exchange=ExchangeEnum.NSE,
                segment=SegmentEnum.EQ,
                instrument_type=InstrumentType.EQ,
            ),
            _make_instrument(
                instrument_id="BSE:RELIANCE:EQ",
                trading_symbol="RELIANCE",
                exchange=ExchangeEnum.BSE,
                segment=SegmentEnum.EQ,
                instrument_type=InstrumentType.EQ,
            ),
            _make_instrument(
                instrument_id="NFO:NIFTY25JANFUT:FUTIDX",
                trading_symbol="NIFTY25JANFUT",
                exchange=ExchangeEnum.NFO,
                segment=SegmentEnum.FO,
                instrument_type=InstrumentType.FUTIDX,
                underlying="NIFTY",
                expiry=date(2025, 1, 30),
            ),
            _make_instrument(
                instrument_id="NFO:NIFTY25JAN22000CE:OPTIDX",
                trading_symbol="NIFTY25JAN22000CE",
                exchange=ExchangeEnum.NFO,
                segment=SegmentEnum.FO,
                instrument_type=InstrumentType.OPTIDX,
                underlying="NIFTY",
                expiry=date(2025, 1, 30),
                option_type="CE",
                strike=22000.0,
            ),
            _make_instrument(
                instrument_id="NFO:BANKNIFTY25FEB44000PE:OPTIDX",
                trading_symbol="BANKNIFTY25FEB44000PE",
                exchange=ExchangeEnum.NFO,
                segment=SegmentEnum.FO,
                instrument_type=InstrumentType.OPTIDX,
                underlying="BANKNIFTY",
                expiry=date(2025, 2, 27),
                option_type="PE",
                strike=44000.0,
            ),
        ]
        return _make_service(*instruments)

    def test_filter_by_exchange_nse(self):
        svc = self._build_universe()
        results = svc.search_instruments(exchange=ExchangeEnum.NSE)
        ids = {r.instrumentId for r in results}
        assert "NSE:RELIANCE:EQ" in ids
        assert "NSE:TCS:EQ" in ids
        assert "BSE:RELIANCE:EQ" not in ids
        assert "NFO:NIFTY25JANFUT:FUTIDX" not in ids

    def test_filter_by_exchange_string(self):
        svc = self._build_universe()
        results = svc.search_instruments(exchange="BSE")
        ids = {r.instrumentId for r in results}
        assert "BSE:RELIANCE:EQ" in ids
        assert "NSE:RELIANCE:EQ" not in ids

    def test_filter_by_instrument_type_eq(self):
        svc = self._build_universe()
        results = svc.search_instruments(instrument_type=InstrumentType.EQ)
        ids = {r.instrumentId for r in results}
        assert "NSE:RELIANCE:EQ" in ids
        assert "NSE:TCS:EQ" in ids
        assert "NFO:NIFTY25JANFUT:FUTIDX" not in ids

    def test_filter_by_instrument_type_string(self):
        svc = self._build_universe()
        results = svc.search_instruments(instrument_type="OPTIDX")
        assert all(r.instrumentType == InstrumentType.OPTIDX for r in results)
        assert len(results) == 2

    def test_filter_by_underlying_nifty(self):
        svc = self._build_universe()
        results = svc.search_instruments(underlying="NIFTY")
        ids = {r.instrumentId for r in results}
        assert "NFO:NIFTY25JANFUT:FUTIDX" in ids
        assert "NFO:NIFTY25JAN22000CE:OPTIDX" in ids
        assert "NFO:BANKNIFTY25FEB44000PE:OPTIDX" not in ids

    def test_filter_by_underlying_banknifty(self):
        svc = self._build_universe()
        results = svc.search_instruments(underlying="BANKNIFTY")
        assert len(results) == 1
        assert results[0].instrumentId == "NFO:BANKNIFTY25FEB44000PE:OPTIDX"

    def test_filter_by_segment_fo(self):
        svc = self._build_universe()
        results = svc.search_instruments(segment=SegmentEnum.FO)
        assert all(r.segment == SegmentEnum.FO for r in results)
        assert len(results) == 3

    def test_filter_by_segment_string(self):
        svc = self._build_universe()
        results = svc.search_instruments(segment="EQ")
        assert all(r.segment == SegmentEnum.EQ for r in results)
        assert len(results) == 3

    def test_filter_by_expiry(self):
        svc = self._build_universe()
        results = svc.search_instruments(expiry=date(2025, 1, 30))
        ids = {r.instrumentId for r in results}
        assert "NFO:NIFTY25JANFUT:FUTIDX" in ids
        assert "NFO:NIFTY25JAN22000CE:OPTIDX" in ids
        assert "NFO:BANKNIFTY25FEB44000PE:OPTIDX" not in ids

    def test_combined_filters_underlying_and_type(self):
        svc = self._build_universe()
        results = svc.search_instruments(
            underlying="NIFTY",
            instrument_type=InstrumentType.OPTIDX,
        )
        assert len(results) == 1
        assert results[0].instrumentId == "NFO:NIFTY25JAN22000CE:OPTIDX"

    def test_combined_filters_exchange_and_segment(self):
        svc = self._build_universe()
        results = svc.search_instruments(exchange=ExchangeEnum.NFO, segment=SegmentEnum.FO)
        assert len(results) == 3

    def test_no_match_returns_empty(self):
        svc = self._build_universe()
        results = svc.search_instruments(underlying="UNKNOWN_UNDERLYING")
        assert results == []

    def test_no_filters_returns_all_active(self):
        svc = self._build_universe()
        results = svc.search_instruments(active_only=True)
        # All 6 instruments have active_to=None so all are active
        assert len(results) == 6

    def test_no_filters_active_only_false_returns_all(self):
        svc = self._build_universe()
        results = svc.search_instruments(active_only=False)
        assert len(results) == 6

    def test_filter_with_active_only_excludes_expired(self):
        svc = self._build_universe()
        # Add an expired instrument
        expired = _make_instrument(
            instrument_id="NSE:OLDCO:EQ",
            trading_symbol="OLDCO",
            active_from=date(2010, 1, 1),
            active_to=date(2020, 1, 1),
        )
        svc._instruments[expired.instrumentId] = expired

        results = svc.search_instruments(exchange=ExchangeEnum.NSE, active_only=True)
        ids = {r.instrumentId for r in results}
        assert "NSE:OLDCO:EQ" not in ids
        assert "NSE:RELIANCE:EQ" in ids


# ---------------------------------------------------------------------------
# InstrumentMasterService.resolve_provider_tokens
# ---------------------------------------------------------------------------


class TestResolveProviderTokens:
    def _make_svc(self) -> InstrumentMasterService:
        inst = _make_instrument(
            instrument_id="NSE:RELIANCE:EQ",
            angel_token="12345",
            angel_symbol="RELIANCE-EQ",
            upstox_key="NSE_EQ|RELIANCE",
            upstox_symbol="RELIANCE",
        )
        return _make_service(inst)

    def test_angel_one_returns_both_fields(self):
        svc = self._make_svc()
        result = svc.resolve_provider_tokens("NSE:RELIANCE:EQ", "angel_one")
        assert result == {"angelToken": "12345", "angelSymbol": "RELIANCE-EQ"}

    def test_upstox_returns_both_fields(self):
        svc = self._make_svc()
        result = svc.resolve_provider_tokens("NSE:RELIANCE:EQ", "upstox")
        assert result == {"upstoxKey": "NSE_EQ|RELIANCE", "upstoxSymbol": "RELIANCE"}

    def test_unknown_provider_returns_empty_dict(self):
        svc = self._make_svc()
        result = svc.resolve_provider_tokens("NSE:RELIANCE:EQ", "jugaad")
        assert result == {}

    def test_missing_instrument_returns_empty_dict(self):
        svc = self._make_svc()
        result = svc.resolve_provider_tokens("NSE:NONEXISTENT:EQ", "angel_one")
        assert result == {}

    def test_tokens_are_none_when_not_mapped(self):
        """Instrument without any token mappings returns None values."""
        inst = _make_instrument(instrument_id="NSE:NIFTY:IDX")  # no tokens set
        svc = _make_service(inst)
        result = svc.resolve_provider_tokens("NSE:NIFTY:IDX", "angel_one")
        assert result == {"angelToken": None, "angelSymbol": None}

    def test_upstox_tokens_are_none_when_not_mapped(self):
        inst = _make_instrument(instrument_id="NSE:NIFTY:IDX")
        svc = _make_service(inst)
        result = svc.resolve_provider_tokens("NSE:NIFTY:IDX", "upstox")
        assert result == {"upstoxKey": None, "upstoxSymbol": None}

    def test_result_does_not_expose_in_public_dump(self):
        """Ensure the returned token dict is NOT the model_dump output."""
        svc = self._make_svc()
        token_result = svc.resolve_provider_tokens("NSE:RELIANCE:EQ", "angel_one")
        # The keys in the token result must NOT appear in model_dump
        instrument = svc.get_instrument("NSE:RELIANCE:EQ")
        assert instrument is not None
        public_dump = instrument.model_dump()
        for key in token_result:
            assert key not in public_dump, (
                f"Provider token field '{key}' leaked into public model dump"
            )

    def test_provider_id_case_insensitive(self):
        svc = self._make_svc()
        # Uppercase ANGEL_ONE should match
        result_upper = svc.resolve_provider_tokens("NSE:RELIANCE:EQ", "ANGEL_ONE")
        result_lower = svc.resolve_provider_tokens("NSE:RELIANCE:EQ", "angel_one")
        assert result_upper == result_lower

    def test_provider_id_mixed_case(self):
        svc = self._make_svc()
        result = svc.resolve_provider_tokens("NSE:RELIANCE:EQ", "Angel_One")
        assert result == {"angelToken": "12345", "angelSymbol": "RELIANCE-EQ"}


# ---------------------------------------------------------------------------
# InstrumentMasterService.load_from_db
# ---------------------------------------------------------------------------


def _make_mock_mapping(row_dict: dict) -> MagicMock:
    """Create a MagicMock that mimics a SQLAlchemy RowMapping (dict-like)."""
    mapping = MagicMock()
    mapping.__iter__ = MagicMock(return_value=iter(row_dict.keys()))
    # dict(mapping) should return row_dict — mock the keys() for dict()
    # We need the mock to behave as a plain dict for dict(mapping)
    # The easiest way: make it directly iterable as key-value pairs by
    # overriding the MagicMock with a simple dict-wrapper.
    return row_dict  # just return the plain dict directly; _row_to_instrument accepts dict


def _make_db_row(
    instrument_id: str = "NSE:RELIANCE:EQ",
    trading_symbol: str = "RELIANCE",
    exchange: str = "NSE",
    segment: str = "EQ",
    instrument_type: str = "EQ",
    active_from: date = date(2020, 1, 1),
    active_to: date | None = None,
    angel_token: str | None = None,
    upstox_key: str | None = None,
) -> dict:
    return {
        "instrument_id": instrument_id,
        "trading_symbol": trading_symbol,
        "display_symbol": None,
        "isin": None,
        "exchange": exchange,
        "segment": segment,
        "instrument_type": instrument_type,
        "underlying": None,
        "expiry": None,
        "strike": None,
        "option_type": None,
        "lot_size": 1,
        "tick_size": 0.05,
        "active_from": active_from,
        "active_to": active_to,
        "angel_token": angel_token,
        "angel_symbol": None,
        "upstox_key": upstox_key,
        "upstox_symbol": None,
    }


class TestLoadFromDb:
    def _make_engine_mock(self, rows: list[dict]) -> MagicMock:
        """
        Build a mock AsyncEngine whose connect() returns rows from
        result.mappings().
        """
        # Mock RowMapping objects — SQLAlchemy returns objects supporting
        # `dict(mapping)`. We supply plain dicts (which also behave dict-like)
        # wrapped in a MagicMock that exposes `.mappings()` as an iterable.
        mock_result = MagicMock()
        mock_result.mappings.return_value = rows  # iterate over dicts directly

        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock(return_value=mock_result)

        # engine.connect() is an async context manager
        mock_engine = MagicMock()
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        mock_engine.connect.return_value = mock_cm

        return mock_engine

    @pytest.mark.asyncio
    async def test_loads_instruments_into_memory(self):
        rows = [
            _make_db_row("NSE:RELIANCE:EQ"),
            _make_db_row("NSE:TCS:EQ", trading_symbol="TCS"),
        ]
        engine = self._make_engine_mock(rows)

        svc = InstrumentMasterService()
        assert svc.is_loaded() is False

        await svc.load_from_db(engine)

        assert svc.is_loaded() is True
        assert len(svc) == 2
        assert svc.get_instrument("NSE:RELIANCE:EQ") is not None
        assert svc.get_instrument("NSE:TCS:EQ") is not None

    @pytest.mark.asyncio
    async def test_replaces_previous_data_on_reload(self):
        """A second load_from_db call fully replaces the store."""
        old_rows = [_make_db_row("NSE:OLD:EQ", trading_symbol="OLD")]
        new_rows = [
            _make_db_row("NSE:NEW:EQ", trading_symbol="NEW"),
            _make_db_row("NSE:ALSO_NEW:EQ", trading_symbol="ALSO_NEW"),
        ]

        svc = InstrumentMasterService()
        await svc.load_from_db(self._make_engine_mock(old_rows))
        assert svc.get_instrument("NSE:OLD:EQ") is not None

        await svc.load_from_db(self._make_engine_mock(new_rows))
        assert len(svc) == 2
        assert svc.get_instrument("NSE:OLD:EQ") is None
        assert svc.get_instrument("NSE:NEW:EQ") is not None
        assert svc.get_instrument("NSE:ALSO_NEW:EQ") is not None

    @pytest.mark.asyncio
    async def test_skips_invalid_rows_and_continues(self):
        """A bad row is skipped; valid rows are still loaded."""
        bad_row = {
            "instrument_id": "INVALID",
            "trading_symbol": "INVALID",
            "exchange": "NOT_A_REAL_EXCHANGE",  # will fail enum validation
            "segment": "EQ",
            "instrument_type": "EQ",
            "lot_size": 1,
            "tick_size": 0.05,
            "active_from": date(2020, 1, 1),
            "active_to": None,
        }
        good_row = _make_db_row("NSE:GOOD:EQ")
        rows = [bad_row, good_row]

        engine = self._make_engine_mock(rows)
        svc = InstrumentMasterService()
        await svc.load_from_db(engine)

        # Bad row skipped; good row present
        assert len(svc) == 1
        assert svc.get_instrument("NSE:GOOD:EQ") is not None
        assert svc.get_instrument("INVALID") is None

    @pytest.mark.asyncio
    async def test_empty_table_results_in_empty_store(self):
        engine = self._make_engine_mock([])
        svc = InstrumentMasterService()
        await svc.load_from_db(engine)
        assert svc.is_loaded() is False
        assert len(svc) == 0

    @pytest.mark.asyncio
    async def test_provider_tokens_loaded_correctly(self):
        rows = [
            _make_db_row(
                "NSE:RELIANCE:EQ",
                angel_token="12345",
                upstox_key="NSE_EQ|RELIANCE",
            )
        ]
        engine = self._make_engine_mock(rows)
        svc = InstrumentMasterService()
        await svc.load_from_db(engine)

        tokens = svc.resolve_provider_tokens("NSE:RELIANCE:EQ", "angel_one")
        assert tokens["angelToken"] == "12345"

        upstox = svc.resolve_provider_tokens("NSE:RELIANCE:EQ", "upstox")
        assert upstox["upstoxKey"] == "NSE_EQ|RELIANCE"

    @pytest.mark.asyncio
    async def test_load_from_db_calls_correct_sql(self):
        """Verify that SELECT is executed against instrument_master."""
        engine = self._make_engine_mock([])
        svc = InstrumentMasterService()
        await svc.load_from_db(engine)

        # Confirm execute was called (not no-op)
        mock_conn = engine.connect.return_value.__aenter__.return_value
        mock_conn.execute.assert_called_once()
        # The argument should be a SQLAlchemy text() object; check the SQL string
        call_args = mock_conn.execute.call_args[0]
        assert len(call_args) == 1
        sql_text = str(call_args[0])
        assert "instrument_master" in sql_text


# ---------------------------------------------------------------------------
# Helpers: is_loaded / __len__
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_is_loaded_false_when_empty(self):
        svc = InstrumentMasterService()
        assert svc.is_loaded() is False

    def test_is_loaded_true_when_populated(self):
        svc = _make_service(_make_instrument())
        assert svc.is_loaded() is True

    def test_len_zero_when_empty(self):
        svc = InstrumentMasterService()
        assert len(svc) == 0

    def test_len_matches_loaded_count(self):
        svc = _make_service(
            _make_instrument(instrument_id="NSE:A:EQ", trading_symbol="A"),
            _make_instrument(instrument_id="NSE:B:EQ", trading_symbol="B"),
            _make_instrument(instrument_id="NSE:C:EQ", trading_symbol="C"),
        )
        assert len(svc) == 3
