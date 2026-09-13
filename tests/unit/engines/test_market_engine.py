"""
tests/unit/engines/test_market_engine.py

Unit tests for src/engines/market_engine.py — MarketEngine.

Covers:
  Task 6.3 — Live quote acquisition and normalisation
    - get_live_quote returns all required fields during REGULAR session
    - get_live_quote returns cached quote during CLOSED session (not a failure)
    - get_live_quote returns CLOSED status without incrementing any error counters
    - oi is NEVER populated from tradedValue (null + oiMissing=True when absent)
    - bid/ask are null when not provided (zero is not a substitute)
    - get_current_session_phase convenience wrapper
    - Empty quote sentinel structure when no cache available

  Task 6.4 — Option chain pipeline
    - get_option_chain returns rows:[] with marketStatus:"CLOSED" during CLOSED session
    - get_option_chain returns rows:[] with marketStatus:"NO_DATA" when no contracts
    - chainQuality is "DEGRADED" when spot price is older than 60 seconds
    - spotAgeMs is included when spot is stale
    - Analytics: pcrOi, pcrVolume, maxCeOiStrike, maxPeOiStrike, totalCeOi, totalPeOi, atmIv, maxPain
    - Analytics have correct MetricTag annotations (DERIVED)
    - iv is null when absent (zero is not a substitute)
    - Greeks are null when absent
    - Analytics _compute_max_pain logic
    - pcrOi is null when totalCeOi == 0

Requirements: 3.1, 3.2, 3.3, 3.4, 3.7, 3.8, 3.9, 3.10, 12.5
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from src.core.normaliser import MetricTag
from src.core.schemas.instrument import SessionPhase
from src.engines.market_engine import (
    MarketEngine,
    _compute_analytics,
    _compute_max_pain,
    _empty_analytics,
    _empty_quote,
)

_IST = ZoneInfo("Asia/Kolkata")
_UTC = timezone.utc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ist_datetime(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    """Create an IST datetime and return it as a UTC-aware datetime."""
    from datetime import datetime as dt
    ist_dt = dt(year, month, day, hour, minute, 0, tzinfo=_IST)
    return ist_dt.astimezone(_UTC)


def _make_engine_with_phase(phase: SessionPhase) -> MarketEngine:
    """Create a MarketEngine whose session engine always returns *phase*."""
    mock_session = MagicMock()
    mock_session.get_current_phase.return_value = phase
    mock_session.is_trading_day.return_value = phase != SessionPhase.CLOSED
    return MarketEngine(session_engine=mock_session)


def _make_mock_calendar(is_trading: bool = True) -> MagicMock:
    """Create a mock HolidayCalendar."""
    cal = MagicMock()
    cal.is_trading_day.return_value = is_trading
    cal.next_trading_day.return_value = None  # unused in this suite
    cal.calendar_status = "2025-01-01"
    cal.get_holidays_for_month.return_value = []
    return cal


# ---------------------------------------------------------------------------
# Tests: get_current_session_phase
# ---------------------------------------------------------------------------


class TestGetCurrentSessionPhase:
    def test_delegates_to_session_engine(self) -> None:
        """get_current_session_phase forwards to the session engine."""
        engine = _make_engine_with_phase(SessionPhase.REGULAR)
        assert engine.get_current_session_phase() == SessionPhase.REGULAR

    def test_returns_closed_during_weekend(self) -> None:
        """Engine returns CLOSED during weekends."""
        # Saturday 09:30 IST → should be CLOSED
        engine = MarketEngine()
        saturday_utc = _ist_datetime(2025, 1, 4, 9, 30)  # Saturday
        phase = engine.get_current_session_phase(saturday_utc)
        assert phase == SessionPhase.CLOSED

    def test_returns_regular_on_trading_day_at_10am(self) -> None:
        """Engine returns REGULAR on a weekday at 10:00 IST."""
        engine = MarketEngine()
        monday_utc = _ist_datetime(2025, 1, 6, 10, 0)  # Monday
        phase = engine.get_current_session_phase(monday_utc)
        assert phase == SessionPhase.REGULAR


# ---------------------------------------------------------------------------
# Tests: get_live_quote — REGULAR session
# ---------------------------------------------------------------------------


class TestGetLiveQuoteRegular:
    @pytest.mark.asyncio
    async def test_returns_required_fields(self) -> None:
        """Live quote includes all required fields."""
        required_fields = [
            "instrumentId", "symbol", "exchange", "ltp",
            "open", "high", "low", "prevClose", "change", "changePct",
            "volume", "oi", "tradedValue", "totalBuyQty", "totalSellQty",
            "upperCircuit", "lowerCircuit", "weekHigh52", "weekLow52",
            "lastTradeTime", "bid", "ask", "marketStatus", "provenance",
        ]
        engine = _make_engine_with_phase(SessionPhase.REGULAR)
        quote = await engine.get_live_quote("NSE:NIFTY:IDX", exchange="NSE")

        for field in required_fields:
            assert field in quote, f"Required field '{field}' missing from quote"

    @pytest.mark.asyncio
    async def test_market_status_is_regular(self) -> None:
        """Quote from REGULAR session has marketStatus='REGULAR'."""
        engine = _make_engine_with_phase(SessionPhase.REGULAR)
        quote = await engine.get_live_quote("NSE:NIFTY:IDX")
        assert quote["marketStatus"] == SessionPhase.REGULAR.value

    @pytest.mark.asyncio
    async def test_oi_missing_when_not_provided(self) -> None:
        """oi is null and oiMissing=True when provider does not supply OI."""
        engine = _make_engine_with_phase(SessionPhase.REGULAR)
        quote = await engine.get_live_quote("NSE:NIFTY:IDX")
        # Stub returns oi=None → should be null + oiMissing=True
        assert quote["oi"] is None
        assert quote["oiMissing"] is True

    @pytest.mark.asyncio
    async def test_bid_ask_null_when_not_provided(self) -> None:
        """bid and ask are null when provider does not supply them."""
        engine = _make_engine_with_phase(SessionPhase.REGULAR)
        quote = await engine.get_live_quote("NSE:NIFTY:IDX")
        assert quote["bid"] is None
        assert quote["ask"] is None

    @pytest.mark.asyncio
    async def test_provenance_is_dict(self) -> None:
        """Provenance is a dict with required keys."""
        engine = _make_engine_with_phase(SessionPhase.REGULAR)
        quote = await engine.get_live_quote("NSE:RELIANCE:EQ")
        prov = quote["provenance"]
        assert isinstance(prov, dict)
        assert "dataObservationId" in prov
        assert "receivedAtMs" in prov
        assert "normalisationVersion" in prov

    @pytest.mark.asyncio
    async def test_quote_is_cached_after_regular_call(self) -> None:
        """A quote fetched during REGULAR is stored in _quote_cache."""
        engine = _make_engine_with_phase(SessionPhase.REGULAR)

        # Override stub to return a valid ltp so normalisation succeeds
        async def _valid_stub(instrument_id: str, exchange: str) -> dict:
            return {
                "instrumentId": instrument_id,
                "symbol": "NIFTY",
                "exchange": exchange,
                "ltp": 22000.0,
            }

        engine._fetch_live_quote_stub = _valid_stub  # type: ignore[assignment]
        await engine.get_live_quote("NSE:NIFTY:IDX", exchange="NSE")
        assert "NSE:NSE:NIFTY:IDX" in engine._quote_cache

    @pytest.mark.asyncio
    async def test_oi_never_from_traded_value(self) -> None:
        """oi is NEVER populated from tradedValue — semantic integrity."""
        engine = _make_engine_with_phase(SessionPhase.REGULAR)

        # Inject a stub that returns oi == tradedValue (substitution attempt)
        async def _bad_stub(instrument_id: str, exchange: str) -> dict:
            return {
                "instrumentId": instrument_id,
                "symbol": "NIFTY",
                "exchange": exchange,
                "ltp": 22000.0,
                "oi": 1234567.0,        # equals tradedValue below
                "tradedValue": 1234567.0,  # same value → must be rejected
            }

        engine._fetch_live_quote_stub = _bad_stub  # type: ignore[assignment]
        quote = await engine.get_live_quote("NSE:NIFTY:IDX")
        # Normaliser rejects oi = tradedValue substitution
        assert quote["oi"] is None
        assert quote["oiMissing"] is True


# ---------------------------------------------------------------------------
# Tests: get_live_quote — non-REGULAR sessions
# ---------------------------------------------------------------------------


class TestGetLiveQuoteNonRegular:
    async def _populate_cache(self, engine: MarketEngine, instrument_id: str, exchange: str) -> None:
        """Helper: populate the quote cache via a valid REGULAR-phase fetch."""
        async def _valid_stub(iid: str, exch: str) -> dict:
            return {
                "instrumentId": iid,
                "symbol": iid.split(":")[-1] if ":" in iid else iid,
                "exchange": exch,
                "ltp": 22000.0,
            }
        engine._fetch_live_quote_stub = _valid_stub  # type: ignore[assignment]
        await engine.get_live_quote(instrument_id, exchange=exchange)

    @pytest.mark.asyncio
    async def test_closed_returns_last_cached_quote(self) -> None:
        """During CLOSED session, the last cached quote is returned."""
        engine = _make_engine_with_phase(SessionPhase.REGULAR)
        await self._populate_cache(engine, "NSE:NIFTY:IDX", "NSE")

        engine._session_engine.get_current_phase.return_value = SessionPhase.CLOSED
        quote = await engine.get_live_quote("NSE:NIFTY:IDX", exchange="NSE")
        assert quote["marketStatus"] == SessionPhase.CLOSED.value

    @pytest.mark.asyncio
    async def test_closed_with_no_cache_returns_empty_sentinel(self) -> None:
        """CLOSED session with no cache returns sentinel with null fields."""
        engine = _make_engine_with_phase(SessionPhase.CLOSED)
        quote = await engine.get_live_quote("NSE:NIFTY:IDX", exchange="NSE")
        assert quote["marketStatus"] == SessionPhase.CLOSED.value
        assert quote["ltp"] is None
        assert "instrumentId" in quote

    @pytest.mark.asyncio
    async def test_pre_open_returns_cached_with_correct_status(self) -> None:
        """PRE_OPEN session updates marketStatus in cached quote."""
        engine = _make_engine_with_phase(SessionPhase.REGULAR)
        await self._populate_cache(engine, "NSE:NIFTY:IDX", "NSE")

        engine._session_engine.get_current_phase.return_value = SessionPhase.PRE_OPEN
        quote = await engine.get_live_quote("NSE:NIFTY:IDX", exchange="NSE")
        assert quote["marketStatus"] == SessionPhase.PRE_OPEN.value

    @pytest.mark.asyncio
    async def test_post_market_returns_cached_with_correct_status(self) -> None:
        """POST_MARKET updates marketStatus in the cached quote."""
        engine = _make_engine_with_phase(SessionPhase.REGULAR)
        await self._populate_cache(engine, "NSE:NIFTY:IDX", "NSE")

        engine._session_engine.get_current_phase.return_value = SessionPhase.POST_MARKET
        quote = await engine.get_live_quote("NSE:NIFTY:IDX", exchange="NSE")
        assert quote["marketStatus"] == SessionPhase.POST_MARKET.value

    @pytest.mark.asyncio
    async def test_expired_cache_returns_empty_sentinel(self) -> None:
        """Cached quote older than 24 hours is not served."""
        engine = _make_engine_with_phase(SessionPhase.REGULAR)

        # Override stub to return a valid ltp so normalisation succeeds and cache populates
        async def _valid_stub(instrument_id: str, exchange: str) -> dict:
            return {
                "instrumentId": instrument_id,
                "symbol": "NIFTY",
                "exchange": exchange,
                "ltp": 22000.0,
            }

        engine._fetch_live_quote_stub = _valid_stub  # type: ignore[assignment]
        await engine.get_live_quote("NSE:NIFTY:IDX", exchange="NSE")

        # Artificially age the cache entry beyond 24 hours
        cache_key = "NSE:NSE:NIFTY:IDX"
        entry = engine._quote_cache[cache_key]
        entry["ts_sec"] = time.monotonic() - (25 * 3600)  # 25 hours ago

        engine._session_engine.get_current_phase.return_value = SessionPhase.CLOSED
        quote = await engine.get_live_quote("NSE:NIFTY:IDX", exchange="NSE")

        # Expired cache → empty sentinel with null ltp
        assert quote["ltp"] is None
        assert quote["marketStatus"] == SessionPhase.CLOSED.value


# ---------------------------------------------------------------------------
# Tests: _empty_quote sentinel
# ---------------------------------------------------------------------------


class TestEmptyQuote:
    def test_all_required_fields_present(self) -> None:
        q = _empty_quote("NSE:NIFTY:IDX", "NSE", "CLOSED")
        required = [
            "instrumentId", "symbol", "exchange", "ltp", "open", "high",
            "low", "prevClose", "change", "changePct", "volume", "oi",
            "tradedValue", "totalBuyQty", "totalSellQty", "upperCircuit",
            "lowerCircuit", "weekHigh52", "weekLow52", "lastTradeTime",
            "bid", "ask", "marketStatus", "provenance",
        ]
        for f in required:
            assert f in q, f"Field '{f}' missing from empty quote"

    def test_oi_is_null(self) -> None:
        q = _empty_quote("NSE:NIFTY:IDX", "NSE", "CLOSED")
        assert q["oi"] is None
        assert q["oiMissing"] is True

    def test_bid_ask_are_null(self) -> None:
        q = _empty_quote("NSE:NIFTY:IDX", "NSE", "CLOSED")
        assert q["bid"] is None
        assert q["ask"] is None
        assert q["bidAskMissing"] is True

    def test_market_status_matches_arg(self) -> None:
        q = _empty_quote("X", "Y", "MY_STATUS")
        assert q["marketStatus"] == "MY_STATUS"

    def test_instrument_id_and_exchange_preserved(self) -> None:
        q = _empty_quote("NSE:RELIANCE:EQ", "NSE", "CLOSED")
        assert q["instrumentId"] == "NSE:RELIANCE:EQ"
        assert q["exchange"] == "NSE"


# ---------------------------------------------------------------------------
# Tests: get_option_chain — CLOSED session
# ---------------------------------------------------------------------------


class TestGetOptionChainClosed:
    @pytest.mark.asyncio
    async def test_closed_session_returns_empty_rows(self) -> None:
        """CLOSED session: rows=[], marketStatus='CLOSED', not HTTP 5xx."""
        engine = _make_engine_with_phase(SessionPhase.CLOSED)
        chain = await engine.get_option_chain("NIFTY", expiry="2025-01-30")
        assert chain["rows"] == []
        assert chain["marketStatus"] == SessionPhase.CLOSED.value

    @pytest.mark.asyncio
    async def test_closed_session_underlying_preserved(self) -> None:
        """CLOSED response contains the requested underlying."""
        engine = _make_engine_with_phase(SessionPhase.CLOSED)
        chain = await engine.get_option_chain("BANKNIFTY")
        assert chain["underlying"] == "BANKNIFTY"

    @pytest.mark.asyncio
    async def test_closed_session_has_analytics(self) -> None:
        """CLOSED response includes (empty) analytics dict."""
        engine = _make_engine_with_phase(SessionPhase.CLOSED)
        chain = await engine.get_option_chain("NIFTY")
        assert "analytics" in chain
        assert isinstance(chain["analytics"], dict)


# ---------------------------------------------------------------------------
# Tests: get_option_chain — no data
# ---------------------------------------------------------------------------


class TestGetOptionChainNoData:
    @pytest.mark.asyncio
    async def test_no_contracts_returns_no_data_status(self) -> None:
        """When provider returns no contracts, marketStatus='NO_DATA'."""
        engine = _make_engine_with_phase(SessionPhase.REGULAR)
        # Default stub returns empty rows
        chain = await engine.get_option_chain("NIFTY", expiry="2025-01-30")
        assert chain["rows"] == []
        assert chain["marketStatus"] == "NO_DATA"

    @pytest.mark.asyncio
    async def test_no_data_has_required_keys(self) -> None:
        """NO_DATA response has all expected top-level keys."""
        engine = _make_engine_with_phase(SessionPhase.REGULAR)
        chain = await engine.get_option_chain("NIFTY")
        for key in ("underlying", "expiry", "exchange", "rows", "analytics",
                    "chainQuality", "marketStatus", "provenance"):
            assert key in chain, f"Key '{key}' missing from NO_DATA chain response"


# ---------------------------------------------------------------------------
# Tests: get_option_chain — with data
# ---------------------------------------------------------------------------


def _make_option_rows() -> list[dict]:
    """Build a minimal set of valid option chain rows for analytics tests."""
    return [
        # CE rows
        {"strike": 22000.0, "optionType": "CE", "ltp": 150.0, "oi": 5000,
         "oiChange": 100, "volume": 200, "tradedValue": 30000.0,
         "iv": None, "bid": None, "ask": None},
        {"strike": 22100.0, "optionType": "CE", "ltp": 80.0, "oi": 8000,
         "oiChange": 200, "volume": 400, "tradedValue": 32000.0,
         "iv": None, "bid": None, "ask": None},
        # PE rows
        {"strike": 22000.0, "optionType": "PE", "ltp": 90.0, "oi": 6000,
         "oiChange": 50, "volume": 300, "tradedValue": 27000.0,
         "iv": None, "bid": None, "ask": None},
        {"strike": 22100.0, "optionType": "PE", "ltp": 160.0, "oi": 4000,
         "oiChange": 80, "volume": 150, "tradedValue": 24000.0,
         "iv": None, "bid": None, "ask": None},
    ]


class TestGetOptionChainWithData:
    @pytest.mark.asyncio
    async def test_rows_are_normalised(self) -> None:
        """Each returned row has required completeness flags."""
        engine = _make_engine_with_phase(SessionPhase.REGULAR)
        rows = _make_option_rows()

        async def _stub(underlying, expiry, exchange):
            return rows, 22050.0, "2025-01-30"

        engine._fetch_option_chain_stub = _stub  # type: ignore[assignment]
        chain = await engine.get_option_chain("NIFTY", expiry="2025-01-30")

        for row in chain["rows"]:
            assert "oiMissing" in row
            assert "ivMissing" in row
            assert "greeksMissing" in row
            assert "bidAskMissing" in row

    @pytest.mark.asyncio
    async def test_iv_null_when_not_provided(self) -> None:
        """iv is null for each row when the provider does not supply it."""
        engine = _make_engine_with_phase(SessionPhase.REGULAR)
        rows = _make_option_rows()

        async def _stub(underlying, expiry, exchange):
            return rows, 22050.0, "2025-01-30"

        engine._fetch_option_chain_stub = _stub  # type: ignore[assignment]
        chain = await engine.get_option_chain("NIFTY")

        for row in chain["rows"]:
            assert row["iv"] is None, "iv must be null when absent"
            assert row["ivMissing"] is True

    @pytest.mark.asyncio
    async def test_greeks_null_when_not_provided(self) -> None:
        """Greeks are null for each row when not provided."""
        engine = _make_engine_with_phase(SessionPhase.REGULAR)
        rows = _make_option_rows()

        async def _stub(underlying, expiry, exchange):
            return rows, 22050.0, "2025-01-30"

        engine._fetch_option_chain_stub = _stub  # type: ignore[assignment]
        chain = await engine.get_option_chain("NIFTY")

        for row in chain["rows"]:
            for greek in ("delta", "gamma", "theta", "vega", "rho"):
                assert row[greek] is None, f"{greek} must be null when absent"

    @pytest.mark.asyncio
    async def test_chain_quality_ok_when_spot_fresh(self) -> None:
        """chainQuality is 'OK' when spot price is fresh."""
        engine = _make_engine_with_phase(SessionPhase.REGULAR)
        rows = _make_option_rows()

        async def _stub(underlying, expiry, exchange):
            return rows, 22050.0, "2025-01-30"

        engine._fetch_option_chain_stub = _stub  # type: ignore[assignment]
        # First call — no prior spot cache, so OK
        chain = await engine.get_option_chain("NIFTY")
        assert chain["chainQuality"] == "OK"

    @pytest.mark.asyncio
    async def test_chain_quality_degraded_when_spot_stale(self) -> None:
        """chainQuality is 'DEGRADED' when spot price is older than 60s."""
        engine = _make_engine_with_phase(SessionPhase.REGULAR)
        rows = _make_option_rows()

        async def _stub(underlying, expiry, exchange):
            return rows, 22050.0, "2025-01-30"

        engine._fetch_option_chain_stub = _stub  # type: ignore[assignment]

        # Pre-seed the spot cache with an old timestamp
        engine._spot_cache["NSE:NIFTY"] = {
            "price": 22050.0,
            "ts_sec": time.monotonic() - 70,  # 70 seconds ago → stale
        }

        chain = await engine.get_option_chain("NIFTY", exchange="NSE")
        assert chain["chainQuality"] == "DEGRADED"
        assert "spotAgeMs" in chain
        assert chain["spotAgeMs"] >= 70_000  # at least 70 000 ms

    @pytest.mark.asyncio
    async def test_analytics_present(self) -> None:
        """Analytics dict is present with expected keys."""
        engine = _make_engine_with_phase(SessionPhase.REGULAR)
        rows = _make_option_rows()

        async def _stub(underlying, expiry, exchange):
            return rows, 22050.0, "2025-01-30"

        engine._fetch_option_chain_stub = _stub  # type: ignore[assignment]
        chain = await engine.get_option_chain("NIFTY")

        analytics = chain["analytics"]
        for key in ("pcrOi", "pcrVolume", "maxCeOiStrike", "maxPeOiStrike",
                    "totalCeOi", "totalPeOi", "atmIv", "maxPain"):
            assert key in analytics, f"Analytics key '{key}' missing"

    @pytest.mark.asyncio
    async def test_analytics_metric_tags_are_derived(self) -> None:
        """All analytics fields are tagged as DERIVED."""
        engine = _make_engine_with_phase(SessionPhase.REGULAR)
        rows = _make_option_rows()

        async def _stub(underlying, expiry, exchange):
            return rows, 22050.0, "2025-01-30"

        engine._fetch_option_chain_stub = _stub  # type: ignore[assignment]
        chain = await engine.get_option_chain("NIFTY")

        tags = chain["analytics"].get("_metricTags", {})
        for field in ("pcrOi", "pcrVolume", "maxCeOiStrike", "maxPeOiStrike",
                      "totalCeOi", "totalPeOi", "atmIv", "maxPain"):
            assert tags.get(field) == MetricTag.DERIVED.value, (
                f"Analytics field '{field}' should be DERIVED, got {tags.get(field)}"
            )


# ---------------------------------------------------------------------------
# Tests: _compute_analytics unit tests
# ---------------------------------------------------------------------------


class TestComputeAnalytics:
    def _make_rows(self) -> list[dict]:
        return [
            {"strike": 22000.0, "optionType": "CE", "ltp": 150.0, "oi": 5000,
             "volume": 200, "iv": 0.18, "bid": None, "ask": None,
             "oiMissing": False, "volumeMissing": False, "ivMissing": False},
            {"strike": 22100.0, "optionType": "CE", "ltp": 80.0, "oi": 8000,
             "volume": 400, "iv": 0.17, "bid": None, "ask": None,
             "oiMissing": False, "volumeMissing": False, "ivMissing": False},
            {"strike": 22000.0, "optionType": "PE", "ltp": 90.0, "oi": 6000,
             "volume": 300, "iv": 0.19, "bid": None, "ask": None,
             "oiMissing": False, "volumeMissing": False, "ivMissing": False},
            {"strike": 22100.0, "optionType": "PE", "ltp": 160.0, "oi": 4000,
             "volume": 150, "iv": 0.20, "bid": None, "ask": None,
             "oiMissing": False, "volumeMissing": False, "ivMissing": False},
        ]

    def test_total_ce_oi(self) -> None:
        rows = self._make_rows()
        analytics = _compute_analytics(rows, spot_price=22050.0)
        assert analytics["totalCeOi"] == 5000 + 8000  # 13000

    def test_total_pe_oi(self) -> None:
        rows = self._make_rows()
        analytics = _compute_analytics(rows, spot_price=22050.0)
        assert analytics["totalPeOi"] == 6000 + 4000  # 10000

    def test_pcr_oi(self) -> None:
        rows = self._make_rows()
        analytics = _compute_analytics(rows, spot_price=22050.0)
        expected = round(10000 / 13000, 4)
        assert analytics["pcrOi"] == pytest.approx(expected, rel=1e-4)

    def test_pcr_volume(self) -> None:
        rows = self._make_rows()
        analytics = _compute_analytics(rows, spot_price=22050.0)
        expected = round((300 + 150) / (200 + 400), 4)
        assert analytics["pcrVolume"] == pytest.approx(expected, rel=1e-4)

    def test_max_ce_oi_strike(self) -> None:
        rows = self._make_rows()
        analytics = _compute_analytics(rows, spot_price=22050.0)
        # CE: strike 22100 has oi=8000 > strike 22000 oi=5000
        assert analytics["maxCeOiStrike"] == 22100.0

    def test_max_pe_oi_strike(self) -> None:
        rows = self._make_rows()
        analytics = _compute_analytics(rows, spot_price=22050.0)
        # PE: strike 22000 has oi=6000 > strike 22100 oi=4000
        assert analytics["maxPeOiStrike"] == 22000.0

    def test_atm_iv_closest_to_spot(self) -> None:
        rows = self._make_rows()
        # spot = 22050 → closest strikes are 22000 and 22100, equidistant
        # min() will pick the first one found in the list (22000 CE with iv=0.18)
        analytics = _compute_analytics(rows, spot_price=22050.0)
        # ATM IV should be a valid float
        assert analytics["atmIv"] is not None
        assert isinstance(analytics["atmIv"], float)

    def test_pcr_oi_null_when_no_ce_oi(self) -> None:
        """pcrOi is null when total CE OI is zero."""
        rows = [
            {"strike": 22000.0, "optionType": "CE", "ltp": 50.0, "oi": 0,
             "volume": 100, "iv": None},
            {"strike": 22000.0, "optionType": "PE", "ltp": 100.0, "oi": 5000,
             "volume": 200, "iv": None},
        ]
        analytics = _compute_analytics(rows, spot_price=22000.0)
        assert analytics["pcrOi"] is None

    def test_pcr_oi_null_when_no_rows_have_oi(self) -> None:
        """pcrOi is null when no CE rows have OI data."""
        rows = [
            {"strike": 22000.0, "optionType": "CE", "ltp": 50.0, "oi": None,
             "volume": 100, "iv": None},
            {"strike": 22000.0, "optionType": "PE", "ltp": 100.0, "oi": 5000,
             "volume": 200, "iv": None},
        ]
        analytics = _compute_analytics(rows, spot_price=22000.0)
        assert analytics["pcrOi"] is None

    def test_atm_iv_none_when_no_iv_data(self) -> None:
        """atmIv is null when no rows have IV data."""
        rows = [
            {"strike": 22000.0, "optionType": "CE", "ltp": 50.0, "oi": 1000,
             "volume": 100, "iv": None},
            {"strike": 22000.0, "optionType": "PE", "ltp": 100.0, "oi": 1000,
             "volume": 200, "iv": None},
        ]
        analytics = _compute_analytics(rows, spot_price=22000.0)
        assert analytics["atmIv"] is None

    def test_all_tags_are_derived(self) -> None:
        """All analytics fields carry DERIVED MetricTag."""
        rows = self._make_rows()
        analytics = _compute_analytics(rows, spot_price=22050.0)
        tags = analytics["_metricTags"]
        for field in ("pcrOi", "pcrVolume", "maxCeOiStrike", "maxPeOiStrike",
                      "totalCeOi", "totalPeOi", "atmIv", "maxPain"):
            assert tags[field] == MetricTag.DERIVED.value


# ---------------------------------------------------------------------------
# Tests: _compute_max_pain
# ---------------------------------------------------------------------------


class TestComputeMaxPain:
    def test_simple_case(self) -> None:
        """Max pain is the strike minimising aggregate OI loss."""
        rows = [
            {"strike": 100.0, "optionType": "CE", "oi": 1000},
            {"strike": 100.0, "optionType": "PE", "oi": 2000},
            {"strike": 110.0, "optionType": "CE", "oi": 500},
            {"strike": 110.0, "optionType": "PE", "oi": 3000},
        ]
        result = _compute_max_pain(rows)
        # At S=100: CE loss = 0, PE loss = 0 (no PE is in-the-money)
        # At S=110: CE loss = 0, PE loss = 0 (no PE is in-the-money)
        # Both give 0; max_pain should be one of the strikes
        assert result in (100.0, 110.0)

    def test_returns_none_when_no_rows(self) -> None:
        assert _compute_max_pain([]) is None

    def test_returns_none_when_no_oi(self) -> None:
        rows = [
            {"strike": 100.0, "optionType": "CE", "oi": None},
            {"strike": 100.0, "optionType": "PE", "oi": None},
        ]
        assert _compute_max_pain(rows) is None

    def test_known_max_pain_value(self) -> None:
        """Verify max pain computation with a deterministic dataset."""
        # CE holders at 22100 are ITM when settlement > 22100.
        # PE holders at 22000 are ITM when settlement < 22000.
        # At S=22000: CE loss = 0, PE loss = 0 → best for option sellers
        # At S=22100: CE loss = 0, PE loss = 2000*(22100-22000)=200000
        rows = [
            {"strike": 22100.0, "optionType": "CE", "oi": 1000},
            {"strike": 22000.0, "optionType": "PE", "oi": 2000},
        ]
        result = _compute_max_pain(rows)
        # At S=22000: CE_loss = 1000*(22100-22000)=100000, PE_loss=0, total=100000
        # At S=22100: CE_loss=0, PE_loss = 2000*(22100-22000)=200000, total=200000
        # Min loss is at 22000
        assert result == 22000.0


# ---------------------------------------------------------------------------
# Tests: _empty_analytics
# ---------------------------------------------------------------------------


class TestEmptyAnalytics:
    def test_structure(self) -> None:
        a = _empty_analytics()
        assert a["pcrOi"] is None
        assert a["pcrVolume"] is None
        assert a["totalCeOi"] == 0
        assert a["totalPeOi"] == 0
        assert "_metricTags" in a

    def test_all_tags_derived(self) -> None:
        a = _empty_analytics()
        for tag in a["_metricTags"].values():
            assert tag == MetricTag.DERIVED.value
