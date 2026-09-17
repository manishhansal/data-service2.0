"""
Unit tests for CandleBuilder and TickPersister.
Tests cover: candle aggregation, dedup, out-of-order, finalisation,
point-in-time contract, and OI handling.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from src.engines.candle_builder import (
    CANDLE_INTERVAL_SECONDS,
    CandleBuilder,
    CandleState,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_tick(
    instrument_id: str = "NSE:NIFTY",
    exchange: str = "NSE",
    ltp: float = 23000.0,
    volume: int = 100,
    exchange_ts_ms: int | None = None,
    provider: str = "angel_one",
    oi: int | None = None,
) -> dict[str, Any]:
    if exchange_ts_ms is None:
        exchange_ts_ms = int(time.time() * 1000)
    tick = {
        "instrumentId": instrument_id,
        "exchange": exchange,
        "ltp": ltp,
        "volume": volume,
        "exchange_ts_ms": exchange_ts_ms,
        "provider": provider,
    }
    if oi is not None:
        tick["oi"] = oi
    return tick


def _candle_open_ms(ts_ms: int, interval: str) -> int:
    period_ms = CANDLE_INTERVAL_SECONDS[interval] * 1000
    return (ts_ms // period_ms) * period_ms


# ---------------------------------------------------------------------------
# CandleState tests
# ---------------------------------------------------------------------------

class TestCandleState:
    def _make_state(self) -> CandleState:
        now_ms = int(time.time() * 1000)
        return CandleState(
            instrument_id="NSE:NIFTY",
            exchange="NSE",
            interval="1m",
            candle_time_ms=_candle_open_ms(now_ms, "1m"),
            candle_time_dt=None,  # type: ignore
        )

    def test_first_tick_initialises_ohlc(self) -> None:
        state = self._make_state()
        tick = _make_tick(ltp=23000.0, volume=50)
        state.apply_tick(tick)
        assert state.open == pytest.approx(23000.0)
        assert state.high == pytest.approx(23000.0)
        assert state.low == pytest.approx(23000.0)
        assert state.close == pytest.approx(23000.0)
        assert state.volume == 50
        assert state.tick_count == 1

    def test_second_tick_updates_high_close(self) -> None:
        state = self._make_state()
        state.apply_tick(_make_tick(ltp=23000.0, volume=50))
        state.apply_tick(_make_tick(ltp=23100.0, volume=100))
        assert state.high == pytest.approx(23100.0)
        assert state.close == pytest.approx(23100.0)
        assert state.low == pytest.approx(23000.0)

    def test_low_tracked(self) -> None:
        state = self._make_state()
        state.apply_tick(_make_tick(ltp=23000.0, volume=100))
        state.apply_tick(_make_tick(ltp=22900.0, volume=200))
        assert state.low == pytest.approx(22900.0)

    def test_duplicate_tick_ignored(self) -> None:
        state = self._make_state()
        tick = _make_tick(ltp=23000.0, volume=50, exchange_ts_ms=1700000000000)
        state.apply_tick(tick)
        state.apply_tick(tick)  # exact duplicate
        assert state.tick_count == 1

    def test_oi_tracked(self) -> None:
        state = self._make_state()
        state.apply_tick(_make_tick(ltp=23000.0, volume=50, oi=500000))
        assert state.open_interest == 500000

    def test_oi_change_computed(self) -> None:
        state = self._make_state()
        state.apply_tick(_make_tick(ltp=23000.0, volume=50, oi=500000))
        state.apply_tick(_make_tick(ltp=23010.0, volume=60, oi=510000))
        assert state.oi_change == 10000

    def test_oi_null_not_substituted_with_zero(self) -> None:
        state = self._make_state()
        state.apply_tick(_make_tick(ltp=23000.0, volume=50))
        assert state.open_interest is None

    def test_to_dict_keys(self) -> None:
        state = self._make_state()
        state.apply_tick(_make_tick(ltp=23000.0, volume=50))
        d = state.to_dict()
        assert "instrument_id" in d
        assert "interval_str" in d
        assert "candle_time_ms" in d
        assert "open" in d
        assert "high" in d
        assert "low" in d
        assert "close" in d
        assert "volume" in d
        assert "available_at_ms" in d  # point-in-time field

    def test_low_enforced_le_min_open_close(self) -> None:
        """low must be <= min(open, close) in to_dict output."""
        state = self._make_state()
        state.open = 23100.0
        state.high = 23200.0
        state.low = 23050.0  # less than both open and close
        state.close = 23150.0
        state.tick_count = 5
        d = state.to_dict()
        assert d["low"] <= min(d["open"], d["close"])


# ---------------------------------------------------------------------------
# CandleBuilder tests
# ---------------------------------------------------------------------------

class TestCandleBuilder:
    @pytest.mark.asyncio
    async def test_single_tick_creates_active_candle(self) -> None:
        builder = CandleBuilder(intervals=["1m"])
        tick = _make_tick(ltp=23000.0, volume=100)
        await builder.process_tick(tick)
        partial = builder.get_partial("NSE:NIFTY", "1m")
        assert partial is not None
        assert partial["close"] == pytest.approx(23000.0)

    @pytest.mark.asyncio
    async def test_multiple_intervals_tracked(self) -> None:
        builder = CandleBuilder(intervals=["1m", "5m", "15m"])
        tick = _make_tick()
        await builder.process_tick(tick)
        for iv in ["1m", "5m", "15m"]:
            assert builder.get_partial("NSE:NIFTY", iv) is not None

    @pytest.mark.asyncio
    async def test_candle_finalised_on_new_period(self) -> None:
        finalised = []

        async def _capture(candle: CandleState) -> None:
            finalised.append(candle)

        builder = CandleBuilder(intervals=["1m"])
        builder.add_candle_callback(_capture)

        # Tick in period 1
        t1_ms = _candle_open_ms(int(time.time() * 1000), "1m")
        await builder.process_tick(_make_tick(ltp=23000.0, volume=100, exchange_ts_ms=t1_ms))

        # Tick in period 2 — triggers finalisation of period 1
        t2_ms = t1_ms + CANDLE_INTERVAL_SECONDS["1m"] * 1000
        await builder.process_tick(_make_tick(ltp=23050.0, volume=200, exchange_ts_ms=t2_ms))

        assert len(finalised) == 1
        assert finalised[0].interval == "1m"
        assert finalised[0].finalised is True
        assert finalised[0].close == pytest.approx(23000.0)

    @pytest.mark.asyncio
    async def test_finalised_candle_has_available_at_ms(self) -> None:
        finalised = []

        async def _capture(candle: CandleState) -> None:
            finalised.append(candle)

        builder = CandleBuilder(intervals=["1m"])
        builder.add_candle_callback(_capture)

        t1_ms = _candle_open_ms(int(time.time() * 1000), "1m")
        await builder.process_tick(_make_tick(ltp=23000.0, exchange_ts_ms=t1_ms))

        t2_ms = t1_ms + CANDLE_INTERVAL_SECONDS["1m"] * 1000
        await builder.process_tick(_make_tick(ltp=23050.0, exchange_ts_ms=t2_ms))

        assert finalised[0].available_at_ms is not None

    @pytest.mark.asyncio
    async def test_point_in_time_contract(self) -> None:
        """candle_time_ms <= available_at_ms must always hold."""
        finalised = []

        async def _capture(candle: CandleState) -> None:
            finalised.append(candle)

        builder = CandleBuilder(intervals=["1m"])
        builder.add_candle_callback(_capture)

        t1_ms = _candle_open_ms(int(time.time() * 1000), "1m")
        await builder.process_tick(_make_tick(ltp=23000.0, exchange_ts_ms=t1_ms))
        t2_ms = t1_ms + CANDLE_INTERVAL_SECONDS["1m"] * 1000
        await builder.process_tick(_make_tick(ltp=23050.0, exchange_ts_ms=t2_ms))

        violations = builder.validate_point_in_time()
        assert violations == [], f"Point-in-time violations: {violations}"

    @pytest.mark.asyncio
    async def test_late_tick_discarded(self) -> None:
        builder = CandleBuilder(intervals=["1m"])
        very_old_ms = int(time.time() * 1000) - 200_000  # 200 seconds ago
        await builder.process_tick(_make_tick(exchange_ts_ms=very_old_ms))
        stats = builder.get_stats()
        assert stats["late_ticks_discarded"] == 1

    @pytest.mark.asyncio
    async def test_flush_all_finalises_active_candles(self) -> None:
        finalised = []

        async def _capture(candle: CandleState) -> None:
            finalised.append(candle)

        builder = CandleBuilder(intervals=["1m", "5m"])
        builder.add_candle_callback(_capture)

        await builder.process_tick(_make_tick())
        await builder.flush_all()

        # Both intervals should have been finalised
        assert len(finalised) == 2
        assert all(c.finalised for c in finalised)

    @pytest.mark.asyncio
    async def test_stats(self) -> None:
        builder = CandleBuilder(intervals=["1m"])
        await builder.process_tick(_make_tick())
        stats = builder.get_stats()
        assert stats["ticks_processed"] == 1
        assert stats["active_candles"] == 1

    def test_invalid_interval_raises(self) -> None:
        with pytest.raises(ValueError, match="3m"):
            CandleBuilder(intervals=["1m", "3m"])  # 3m is banned


# ---------------------------------------------------------------------------
# OI Reconciliation tests
# ---------------------------------------------------------------------------

class TestOIReconciliation:
    def test_both_null_returns_unavailable(self) -> None:
        from src.engines.reconciliation_engine import ReconciliationEngine
        engine = ReconciliationEngine()
        result = engine.reconcile_oi(
            provider_a="angel_one",
            oi_a=None,
            oi_a_status="BLOCKED_BY_PROVIDER_PLAN",
            provider_b="upstox",
            oi_b=None,
            oi_b_status="UNAVAILABLE",
            instrument_id="NSE:NIFTY",
            interval="1d",
        )
        assert result["oi_status"] == "NULL_UNAVAILABLE"
        assert result["canonical_oi"] is None

    def test_single_provider_selected(self) -> None:
        from src.engines.reconciliation_engine import ReconciliationEngine
        engine = ReconciliationEngine()
        result = engine.reconcile_oi(
            provider_a="angel_one",
            oi_a=None,
            oi_a_status="BLOCKED_BY_PROVIDER_PLAN",
            provider_b="upstox",
            oi_b=500000,
            oi_b_status="LIVE",
            instrument_id="NSE:NIFTY",
            interval="1d",
        )
        assert result["canonical_oi"] == 500000
        assert result["canonical_provider"] == "upstox"
        assert result["oi_status"] == "SINGLE_PROVIDER"

    def test_matching_oi_confirmed(self) -> None:
        from src.engines.reconciliation_engine import ReconciliationEngine
        engine = ReconciliationEngine()
        result = engine.reconcile_oi(
            provider_a="angel_one",
            oi_a=500000,
            oi_a_status="HISTORICAL",
            provider_b="upstox",
            oi_b=500000,
            oi_b_status="HISTORICAL",
            instrument_id="NSE:NIFTY",
            interval="1d",
        )
        assert result["oi_status"] == "CONFIRMED"
        assert result["oi_deviation_pct"] == pytest.approx(0.0)

    def test_minor_discrepancy(self) -> None:
        from src.engines.reconciliation_engine import ReconciliationEngine
        engine = ReconciliationEngine()
        result = engine.reconcile_oi(
            provider_a="angel_one",
            oi_a=500000,
            oi_a_status="LIVE",
            provider_b="upstox",
            oi_b=506000,  # 1.2% difference → MINOR (>0.5% and ≤2%)
            oi_b_status="LIVE",
            instrument_id="NSE:NIFTY",
            interval="1m",
        )
        assert result["oi_status"] == "MINOR_DISCREPANCY"

    def test_major_discrepancy_detected(self) -> None:
        from src.engines.reconciliation_engine import ReconciliationEngine
        engine = ReconciliationEngine()
        result = engine.reconcile_oi(
            provider_a="angel_one",
            oi_a=500000,
            oi_a_status="LIVE",
            provider_b="upstox",
            oi_b=600000,  # 20% difference
            oi_b_status="LIVE",
            instrument_id="NSE:NIFTY",
            interval="1m",
        )
        assert result["oi_status"] == "MAJOR_DISCREPANCY"
        assert result["oi_deviation_pct"] > 2.0

    def test_oi_null_never_zero(self) -> None:
        """OI=None must propagate as None, never as 0."""
        from src.engines.reconciliation_engine import ReconciliationEngine
        engine = ReconciliationEngine()
        result = engine.reconcile_oi(
            provider_a="angel_one",
            oi_a=None,
            oi_a_status="BLOCKED_BY_PROVIDER_PLAN",
            provider_b="upstox",
            oi_b=None,
            oi_b_status="UNAVAILABLE",
            instrument_id="NFO:NIFTY25OCTFUT",
            interval="1d",
        )
        # Must NOT return 0 — must return None
        assert result["canonical_oi"] is None
        assert result["oi_provider_a"] is None
        assert result["oi_provider_b"] is None
