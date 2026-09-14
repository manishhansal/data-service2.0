"""
Yahoo Finance live test — Phase 9.
Tests real data acquisition for Indian market historical fallback provider.
Market closed on Sunday — testing EOD historical data.
"""
import asyncio
import time
from datetime import date
from src.providers.adapters.yahoo_finance import YahooFinanceAdapter


async def main():
    results = {}
    adapter = YahooFinanceAdapter()

    # Test historical OHLCV (works regardless of market hours)
    test_cases = [
        ("RELIANCE", "1d", date(2024, 1, 10), date(2024, 1, 15)),
        ("HDFCBANK", "1d", date(2024, 1, 10), date(2024, 1, 15)),
        ("NIFTY", "1d", date(2024, 1, 10), date(2024, 1, 15)),  # Index proxy
        ("RELIANCE", "1h", date(2024, 1, 15), date(2024, 1, 15)),
    ]

    for symbol, interval, from_d, to_d in test_cases:
        try:
            hist = await adapter.fetch_historical_ohlcv_with_interval(
                symbol=symbol,
                from_date=from_d,
                to_date=to_d,
                interval=interval,
            )
            sample = hist[0] if hist else {}
            results[f"{symbol}_{interval}"] = {
                "count": len(hist),
                "sample_close": sample.get("close"),
                "sample_time": sample.get("time"),
                "provider": "yahoo",
            }
            print(f"YAHOO {symbol} {interval}: {len(hist)} bars, sample_close={sample.get('close')}")
        except Exception as e:
            results[f"{symbol}_{interval}"] = {"error": str(e)[:100]}
            print(f"YAHOO {symbol} {interval}: ERROR - {str(e)[:80]}")

    print(f"\nTimestamp: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}")
    print("Note: Indian market closed (Sunday) — historical data only")
    return results


if __name__ == "__main__":
    asyncio.run(main())
