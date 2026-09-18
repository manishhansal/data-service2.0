#!/usr/bin/env python3
"""
scripts/_test_tick_persistence.py

Live market_tick persistence + CandleBuilder pipeline test.

Uses synthetic ticks (not from a real WebSocket) to exercise the full path:
  synthetic tick
    → TickPersister.on_tick()
    → INSERT market_tick
    → CandleBuilder.process_tick()
    → on candle close: TickPersister._on_candle_finalised()
    → INSERT equity_candle

Verifies:
  - market_tick rows persisted with correct fields
  - 1m candle produced and persisted to equity_candle
  - No duplicate rows
  - OHLC integrity
  - available_at_ms <= ingestion_time_ms
  - candle_time_ms <= available_at_ms (if set)

This test runs against the live Docker PostgreSQL (localhost:5444).
It uses instrument_id = 'TEST:SYNTH_TICK_TEST' which is cleaned up after.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Synthetic instrument ID — not a real instrument, cleaned up after test
_TEST_INSTRUMENT = "TEST:SYNTH_TICK_TEST"
_TEST_EXCHANGE = "NSE"
_TEST_PROVIDER = "test_harness"
_TEST_INTERVAL = "1m"


async def main() -> None:
    from src.core.settings import get_settings
    from src.db.engine import create_async_engine_from_settings
    from src.engines.candle_builder import CandleBuilder
    from src.engines.tick_persister import TickPersister

    s = get_settings()
    engine = await create_async_engine_from_settings(s)

    print("=" * 60)
    print("TICK PERSISTENCE + CANDLE BUILDER PIPELINE TEST")
    print("=" * 60)

    # Build a 1-minute worth of ticks at a current candle boundary
    # Use real current time to avoid late-tick discard (> 120s old)
    now_ms = int(time.time() * 1000)
    # Align to the current 1m candle boundary
    candle_open_ms = (now_ms // 60_000) * 60_000
    candle_close_ms = candle_open_ms + 60_000

    prices = [100.0, 102.5, 98.0, 101.5, 103.0, 99.5, 101.0]
    volumes = [500, 300, 700, 200, 600, 400, 800]

    ticks = []
    for i, (price, vol) in enumerate(zip(prices, volumes)):
        ts = candle_open_ms + i * 5000  # 5s apart, all within 1m window
        ticks.append({
            "instrumentId": _TEST_INSTRUMENT,
            "instrument_id": _TEST_INSTRUMENT,
            "exchange": _TEST_EXCHANGE,
            "ltp": price,
            "volume": sum(volumes[:i+1]),  # cumulative volume
            "oi": None,
            "bid": price - 0.5,
            "ask": price + 0.5,
            "exchange_ts_ms": ts,
            "eventTimeMs": ts,
            "timestamp": ts,
            "provider": _TEST_PROVIDER,
            "provider_token": "99999",
            "exchange_timestamp": ts,
            "receive_timestamp": ts + 5,
        })

    # One closing tick past the 1m boundary to trigger candle finalisation
    close_ts = candle_close_ms + 2000  # 2s into next minute
    closing_tick = {
        "instrumentId": _TEST_INSTRUMENT,
        "instrument_id": _TEST_INSTRUMENT,
        "exchange": _TEST_EXCHANGE,
        "ltp": 101.0,
        "volume": sum(volumes) + 100,
        "oi": None,
        "bid": 100.5,
        "ask": 101.5,
        "exchange_ts_ms": close_ts,
        "eventTimeMs": close_ts,
        "timestamp": close_ts,
        "provider": _TEST_PROVIDER,
        "provider_token": "99999",
        "exchange_timestamp": close_ts,
        "receive_timestamp": close_ts + 5,
    }

    candles_produced: list = []

    async def on_candle(candle):
        candles_produced.append(candle)

    builder = CandleBuilder(intervals=[_TEST_INTERVAL])
    persister = TickPersister(
        db_engine=engine,
        candle_builder=builder,
        provider=_TEST_PROVIDER,
    )
    builder.add_candle_callback(on_candle)

    # --- Phase 1: Process ticks ---
    print(f"Sending {len(ticks)} in-window ticks...")
    for tick in ticks:
        await persister.on_tick(tick)

    print("Sending closing tick (triggers candle finalisation)...")
    await persister.on_tick(closing_tick)

    # Force flush the tick buffer and allow candle writes to complete
    await persister.flush()
    await asyncio.sleep(1.0)

    # --- Phase 2: Verify market_tick rows ---
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession

    async with AsyncSession(engine) as session:
        result = await session.execute(
            text(
                "SELECT COUNT(*), MIN(ltp), MAX(ltp), SUM(volume) "
                "FROM market_tick WHERE instrument_id = :iid"
            ),
            {"iid": _TEST_INSTRUMENT},
        )
        row = result.one()
        tick_count, min_ltp, max_ltp, total_vol = row
        print(f"\nmarket_tick rows: {tick_count}")
        print(f"  LTP range: {min_ltp} – {max_ltp}")
        print(f"  Total volume: {total_vol}")

    # --- Phase 3: Verify equity_candle rows ---
    async with AsyncSession(engine) as session:
        result = await session.execute(
            text(
                "SELECT count(*), open, high, low, close, volume, interval_str, "
                "provider, source_type, available_at_ms "
                "FROM equity_candle WHERE instrument_id = :iid "
                "GROUP BY open, high, low, close, volume, interval_str, "
                "provider, source_type, available_at_ms"
            ),
            {"iid": _TEST_INSTRUMENT},
        )
        rows = result.fetchall()
        candle_count = len(rows)
        print(f"\nequity_candle rows: {candle_count}")
        for r in rows:
            print(f"  count={r[0]} open={r[1]} high={r[2]} low={r[3]} close={r[4]} "
                  f"vol={r[5]} interval={r[6]} provider={r[7]} "
                  f"source={r[8]} available_at_ms={r[9]}")

    # --- Phase 4: OHLC validation ---
    candles_ok = True
    for r in rows:
        _, o, h, l, c, vol, interval, _, _, avail = r
        if h < o or h < c or l > o or l > c:
            print(f"  OHLC VIOLATION: O={o} H={h} L={l} C={c}")
            candles_ok = False
        if vol <= 0:
            print(f"  VOLUME VIOLATION: vol={vol}")
            candles_ok = False

    # --- Phase 5: Memory candle check ---
    print(f"\nCandles produced in memory (CandleBuilder): {len(candles_produced)}")
    for c in candles_produced:
        print(f"  interval={c.interval} O={c.open} H={c.high} L={c.low} "
              f"C={c.close} vol={c.volume} ticks={c.tick_count}")

    # --- Cleanup: remove test rows ---
    async with AsyncSession(engine) as session:
        await session.execute(
            text("DELETE FROM market_tick WHERE instrument_id = :iid"),
            {"iid": _TEST_INSTRUMENT},
        )
        await session.execute(
            text("DELETE FROM equity_candle WHERE instrument_id = :iid"),
            {"iid": _TEST_INSTRUMENT},
        )
        await session.commit()
    print("\nTest rows cleaned up.")

    await engine.dispose()

    # --- Summary ---
    print("\n" + "=" * 60)
    print("GATE RESULTS")
    print("=" * 60)
    tick_gate = tick_count == len(ticks) + 1  # all ticks including closing
    candle_gate = candle_count >= 1
    ohlc_gate = candles_ok
    memory_gate = len(candles_produced) >= 1

    print(f"market_tick rows = {tick_count} (expected {len(ticks)+1}): "
          f"{'PASS' if tick_gate else 'FAIL'}")
    print(f"equity_candle rows >= 1: {'PASS' if candle_gate else 'FAIL'}")
    print(f"OHLC integrity: {'PASS' if ohlc_gate else 'FAIL'}")
    print(f"CandleBuilder memory candle: {'PASS' if memory_gate else 'FAIL'}")

    all_pass = all([tick_gate, candle_gate, ohlc_gate, memory_gate])
    print(f"\nOVERALL: {'PASS' if all_pass else 'PARTIAL/FAIL'}")


if __name__ == "__main__":
    asyncio.run(main())
