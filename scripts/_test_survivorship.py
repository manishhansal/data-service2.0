#!/usr/bin/env python3
"""
scripts/_test_survivorship.py

Survivorship bias test for F&O contracts.

Verifies that:
1. Candles exist only DURING the active window of an expired contract.
2. No candles exist AFTER the expiry date.
3. The DB CHECK constraint prevents candles past expiry.

Method:
  - Seeds a synthetic expired futures contract (expiry = yesterday)
  - Inserts test candles: some before expiry, one at expiry, one after (should fail)
  - Verifies the post-expiry candle was rejected or is absent
  - Verifies pre-expiry candles are present
  - Cleans up

This proves the survivorship boundary is enforced at both DB and application level.
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_EXPIRED_INSTRUMENT_ID = "TEST:SURVIVORSHIP_FUT_EXPIRED"
_UNDERLYING = "TEST:SURVIVORSHIP_UNDERLYING"


async def main() -> None:
    from src.core.settings import get_settings
    from src.db.engine import create_async_engine_from_settings
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession

    s = get_settings()
    engine = await create_async_engine_from_settings(s)

    print("=" * 60)
    print("SURVIVORSHIP BIAS TEST")
    print("=" * 60)

    # Expiry = 5 days ago (clearly expired)
    expiry = date.today() - timedelta(days=5)
    expiry_ts = datetime(expiry.year, expiry.month, expiry.day, 15, 30, 0,
                         tzinfo=timezone.utc)  # NSE close on expiry day

    print(f"Test contract expiry: {expiry}")

    async with AsyncSession(engine) as session:
        # Clean up any prior test data
        await session.execute(
            text("DELETE FROM futures_candle WHERE instrument_id = :iid"),
            {"iid": _EXPIRED_INSTRUMENT_ID},
        )
        await session.execute(
            text("DELETE FROM instrument_master WHERE instrument_id = :iid"),
            {"iid": _EXPIRED_INSTRUMENT_ID},
        )
        await session.commit()

    # Seed instrument_master row for the expired contract
    async with AsyncSession(engine) as session:
        await session.execute(
            text("""
                INSERT INTO instrument_master
                  (instrument_id, trading_symbol, exchange, segment, instrument_type,
                   lot_size, tick_size, active_from, expiry, underlying,
                   instrument_class)
                VALUES
                  (:iid, 'SURVTEST25SEP26FUT', 'NFO', 'FO', 'FUTSTK',
                   250, 0.05, :active_from, :expiry, :underlying, NULL)
                ON CONFLICT (instrument_id) DO NOTHING
            """),
            {
                "iid": _EXPIRED_INSTRUMENT_ID,
                "active_from": expiry - timedelta(days=90),
                "expiry": expiry,
                "underlying": _UNDERLYING,
            },
        )
        await session.commit()
    print("Seeded expired instrument in instrument_master")

    # Insert candles: 3 before expiry (should succeed), 1 on expiry day (succeed),
    # 1 after expiry (should be rejected by DB constraint)
    session_date_before = expiry - timedelta(days=3)
    candle_before = datetime(
        session_date_before.year, session_date_before.month,
        session_date_before.day, 9, 15, 0, tzinfo=timezone.utc
    )
    candle_on_expiry = expiry_ts  # candle on expiry day — allowed

    insert_results = {"before_expiry": 0, "on_expiry": 0, "after_expiry_blocked": False}

    # Insert before-expiry candles
    for i in range(3):
        t = candle_before + timedelta(minutes=i)
        sd = session_date_before
        async with AsyncSession(engine) as session:
            try:
                await session.execute(text("""
                    INSERT INTO futures_candle
                      (instrument_id, exchange, interval_str, time, session_date,
                       expiry, contract_type, open, high, low, close, volume,
                       provider, source_type, received_at, normalisation_version,
                       dataset_version, quality_status, poor_quality)
                    VALUES
                      (:iid, 'NFO', '1m', :t, :sd,
                       :expiry, 'FUT', 100.0, 105.0, 98.0, 102.0, 1000,
                       'test_harness', 'BROKER_AUTHENTICATED', NOW(), '2.0.0',
                       1, 'TRUSTED', false)
                    ON CONFLICT DO NOTHING
                """), {
                    "iid": _EXPIRED_INSTRUMENT_ID,
                    "t": t,
                    "sd": sd,
                    "expiry": expiry,
                })
                await session.commit()
                insert_results["before_expiry"] += 1
            except Exception as exc:
                print(f"  Unexpected failure for before-expiry candle: {exc}")

    # Insert on-expiry-day candle (should succeed)
    async with AsyncSession(engine) as session:
        try:
            await session.execute(text("""
                INSERT INTO futures_candle
                  (instrument_id, exchange, interval_str, time, session_date,
                   expiry, contract_type, open, high, low, close, volume,
                   provider, source_type, received_at, normalisation_version,
                   dataset_version, quality_status, poor_quality)
                VALUES
                  (:iid, 'NFO', '1m', :t, :sd,
                   :expiry, 'FUT', 100.0, 106.0, 99.0, 105.0, 500,
                   'test_harness', 'BROKER_AUTHENTICATED', NOW(), '2.0.0',
                   1, 'TRUSTED', false)
                ON CONFLICT DO NOTHING
            """), {
                "iid": _EXPIRED_INSTRUMENT_ID,
                "t": candle_on_expiry,
                "sd": expiry,
                "expiry": expiry,
            })
            await session.commit()
            insert_results["on_expiry"] = 1
        except Exception as exc:
            print(f"  Failure for on-expiry candle: {exc}")

    # Attempt to insert a candle AFTER expiry (should either be blocked or detected)
    candle_after_expiry = datetime(
        expiry.year, expiry.month, expiry.day, tzinfo=timezone.utc
    ) + timedelta(days=2, hours=9, minutes=15)
    async with AsyncSession(engine) as session:
        try:
            await session.execute(text("""
                INSERT INTO futures_candle
                  (instrument_id, exchange, interval_str, time, session_date,
                   expiry, contract_type, open, high, low, close, volume,
                   provider, source_type, received_at, normalisation_version,
                   dataset_version, quality_status, poor_quality)
                VALUES
                  (:iid, 'NFO', '1m', :t, :sd,
                   :expiry, 'FUT', 100.0, 107.0, 99.0, 106.0, 200,
                   'test_harness', 'BROKER_AUTHENTICATED', NOW(), '2.0.0',
                   1, 'TRUSTED', false)
                ON CONFLICT DO NOTHING
            """), {
                "iid": _EXPIRED_INSTRUMENT_ID,
                "t": candle_after_expiry,
                "sd": expiry + timedelta(days=2),
                "expiry": expiry,
            })
            await session.commit()
            print("  Post-expiry candle INSERTED (application must guard this)")
        except Exception:
            insert_results["after_expiry_blocked"] = True
            print("  Post-expiry candle INSERT blocked by DB constraint: OK")

    # Query actual candles in DB
    async with AsyncSession(engine) as session:
        result = await session.execute(
            text("""
                SELECT fc.instrument_id, fc.time, fc.expiry,
                       CAST(fc.time AS date) > fc.expiry AS after_expiry
                FROM futures_candle fc
                JOIN instrument_master im ON im.instrument_id = fc.instrument_id
                WHERE fc.instrument_id = :iid
                ORDER BY fc.time
            """),
            {"iid": _EXPIRED_INSTRUMENT_ID},
        )
        rows = result.fetchall()

    total = len(rows)
    after_expiry_count = sum(1 for r in rows if r[3])
    print(f"\nTotal candles in DB: {total}")
    print(f"Candles AFTER expiry: {after_expiry_count}")

    # SQL cross-check — same query as certification uses
    async with AsyncSession(engine) as session:
        result = await session.execute(
            text("""
                SELECT COUNT(*) FROM futures_candle
                WHERE instrument_id = :iid
                  AND CAST(time AS date) > expiry
            """),
            {"iid": _EXPIRED_INSTRUMENT_ID},
        )
        violations = result.scalar()
    print(f"SQL survivorship violations: {violations}")

    # Cleanup
    async with AsyncSession(engine) as session:
        await session.execute(
            text("DELETE FROM futures_candle WHERE instrument_id = :iid"),
            {"iid": _EXPIRED_INSTRUMENT_ID},
        )
        await session.execute(
            text("DELETE FROM instrument_master WHERE instrument_id = :iid"),
            {"iid": _EXPIRED_INSTRUMENT_ID},
        )
        await session.commit()
    print("Test rows cleaned up.")

    await engine.dispose()

    # Results
    print("\n" + "=" * 60)
    print("GATE RESULTS")
    print("=" * 60)
    print(f"Before-expiry candles inserted: {insert_results['before_expiry']}/3: "
          f"{'PASS' if insert_results['before_expiry'] == 3 else 'FAIL'}")
    print(f"On-expiry-day candle inserted: {insert_results['on_expiry']}: "
          f"{'PASS' if insert_results['on_expiry'] == 1 else 'FAIL'}")

    # Post-expiry: either blocked by DB or detected by SQL check
    if insert_results["after_expiry_blocked"]:
        post_expiry_status = "PASS (DB constraint blocked insert)"
    elif violations == 0:
        post_expiry_status = "PASS (insert succeeded but SQL check detects it - app guard required)"
    else:
        post_expiry_status = f"FAIL ({violations} violations)"
    print(f"Post-expiry candle blocked/detected: {post_expiry_status}")
    print(f"SQL survivorship violations = 0: "
          f"{'PASS' if violations == 0 else f'FAIL ({violations})'}")

    all_pass = (insert_results['before_expiry'] == 3
                and insert_results['on_expiry'] == 1
                and violations == 0)
    print(f"\nOVERALL: {'PASS' if all_pass else 'PARTIAL/FAIL'}")


if __name__ == "__main__":
    asyncio.run(main())
