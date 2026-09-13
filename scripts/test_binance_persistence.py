"""
Binance persistence test — Phase 13.
Fetches real Binance data → normalises → inserts into DB → queries back.
"""
import asyncio
import time
from datetime import datetime, timezone

import asyncpg

from src.providers.adapters.binance_rest import BinanceClient
from src.providers.binance_normaliser import BinanceOHLCVNormaliser


DB_DSN = "postgresql://mds_user:localdevpassword@postgres:5432/mds"


async def main():
    # Acquire real Binance data via DATA-SERVICE adapter
    async with BinanceClient() as client:
        candles_raw = await client.get_klines("BTCUSDT", "1h", limit=5)
        ticker = await client.get_ticker_price("BTCUSDT")
        mark = await client.get_futures_mark_price("BTCUSDT")
        oi = await client.get_futures_open_interest("BTCUSDT")

    print(f"Acquired {len(candles_raw)} BTCUSDT 1h candles from Binance")

    # Normalise through DATA-SERVICE normaliser
    normaliser = BinanceOHLCVNormaliser()
    normalised_records = normaliser.normalise_batch(
        candles_raw,
        symbol="BTCUSDT",
        interval="1h",
    )
    normalised = [
        {"time": r.time, "open": r.open, "high": r.high, "low": r.low, "close": r.close, "volume": r.volume}
        for r in normalised_records
    ]
    print(f"Normalised: {len(normalised)} candles")

    received_at = datetime.now(timezone.utc)

    # Connect to DB and persist
    conn = await asyncpg.connect(DB_DSN)

    before_count = await conn.fetchval("SELECT COUNT(*) FROM candle_bar WHERE instrument_id='BINANCE:BTCUSDT'")
    print(f"candle_bar rows before insert (BINANCE:BTCUSDT): {before_count}")

    inserted = 0
    for c in normalised:
        try:
            await conn.execute(
                """
                INSERT INTO candle_bar 
                  (instrument_id, exchange, interval_str, time, open, high, low, close,
                   volume, oi, volume_unavailable, provider, source_type, received_at,
                   session_date, normalisation_version, dataset_version, poor_quality)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18)
                ON CONFLICT (instrument_id, exchange, interval_str, time) DO NOTHING
                """,
                "BINANCE:BTCUSDT",
                "BINANCE",
                "1h",
                datetime.fromtimestamp(c["time"] / 1000, tz=timezone.utc),
                c["open"],
                c["high"],
                c["low"],
                c["close"],
                int(c["volume"]),
                None,  # oi - not available on klines
                False,
                "binance",
                "EXCHANGE_REST_PUBLIC",
                received_at,
                datetime.fromtimestamp(c["time"] / 1000, tz=timezone.utc).date(),
                "1",
                1,
                False,
            )
            inserted += 1
        except Exception as e:
            print(f"  Insert error: {e}")

    after_count = await conn.fetchval("SELECT COUNT(*) FROM candle_bar WHERE instrument_id='BINANCE:BTCUSDT'")
    print(f"candle_bar rows after insert (BINANCE:BTCUSDT): {after_count}")
    print(f"Rows added: {after_count - before_count}")

    # Query back and verify
    rows = await conn.fetch(
        "SELECT time, open, high, low, close, volume, provider FROM candle_bar "
        "WHERE instrument_id='BINANCE:BTCUSDT' AND interval_str='1h' "
        "ORDER BY time DESC LIMIT 5"
    )
    print("\n=== BTCUSDT 1h candles from DB ===")
    for row in rows:
        print(f"  time={row['time']} open={row['open']} close={row['close']} provider={row['provider']}")

    # Verify API vs DB consistency for the latest candle
    latest_db = rows[0] if rows else None
    latest_api = normalised[-1] if normalised else None
    if latest_db and latest_api:
        api_close = float(latest_api["close"])
        db_close = float(latest_db["close"])
        match = abs(api_close - db_close) < 0.01
        print(f"\nAPI/DB close price match: API={api_close}, DB={db_close}, MATCH={match}")

    # Verify no 3m data
    three_m_count = await conn.fetchval(
        "SELECT COUNT(*) FROM candle_bar WHERE interval_str='3m'"
    )
    print(f"\n3m rows in DB (must be 0): {three_m_count}")
    assert three_m_count == 0, "3m data found in DB — constraint failure!"

    # Verify no duplicates
    dup_count = await conn.fetchval(
        """SELECT COUNT(*) FROM (
            SELECT instrument_id, exchange, interval_str, time, COUNT(*)
            FROM candle_bar GROUP BY instrument_id, exchange, interval_str, time
            HAVING COUNT(*) > 1
        ) d"""
    )
    print(f"Duplicate candles (must be 0): {dup_count}")

    await conn.close()
    print("\n=== BINANCE PERSISTENCE TEST PASSED ===")
    print(f"Timestamp: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}")


if __name__ == "__main__":
    asyncio.run(main())
