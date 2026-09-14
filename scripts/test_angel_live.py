"""
Live test of Angel One adapter with real credentials.
Run: python3 scripts/test_angel_live.py
"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# Load .env.local
from dotenv import dotenv_values
env = dotenv_values(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env.local"))
for k, v in env.items():
    if v:
        os.environ.setdefault(k, v)

from src.core.settings import get_settings
from src.providers.adapters.angel_one import AngelOneAdapter


async def run():
    s = get_settings()
    print("MPIN configured:", bool(s.angel_one_mpin))
    a = AngelOneAdapter(
        api_key=s.angel_one_api_key,
        client_id=s.angel_one_client_id,
        totp_secret=s.angel_one_totp_secret,
        mpin=s.angel_one_mpin,
    )

    print("\n=== 1. Authentication ===")
    await a.ensure_authenticated()
    print("AUTH: OK")

    print("\n=== 2. HDFCBANK 1d historical (token 1333) ===")
    candles = await a.fetch_historical_ohlcv(
        "HDFCBANK", "1333",
        "2024-08-01 09:15", "2024-08-12 15:30",
        "1d", "NSE"
    )
    print(f"  Bars: {len(candles)}")
    if candles:
        r = candles[0]
        print(f"  First: time={r['time']} open={r['open']} close={r['close']} provider={r.get('provider')}")
        r = candles[-1]
        print(f"  Last:  time={r['time']} open={r['open']} close={r['close']}")

    print("\n=== 3. NIFTY 5m historical (token 99926000) ===")
    candles5m = await a.fetch_historical_ohlcv(
        "NIFTY", "99926000",
        "2024-08-01 09:15", "2024-08-02 15:30",
        "5m", "NSE"
    )
    print(f"  Bars: {len(candles5m)}")
    if candles5m:
        r = candles5m[0]
        print(f"  First: time={r['time']} open={r['open']} close={r['close']}")

    print("\n=== 4. RELIANCE live quote (token 2885) ===")
    quotes = await a.fetch_live_quote(["2885"], exchange="NSE")
    print(f"  Quotes: {len(quotes)}")
    if quotes:
        q = quotes[0]
        print(f"  ltp={q.get('ltp')} high={q.get('high')} low={q.get('low')} volume={q.get('tradeVolume')} provider={q.get('provider')}")

    await a.close()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(run())
