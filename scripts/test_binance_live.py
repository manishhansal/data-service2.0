"""
Live Binance API test — Phase 8/9/11.
Tests real connectivity and data acquisition using DATA-SERVICE adapter.
"""
import asyncio
import time
from src.providers.adapters.binance_rest import BinanceClient


async def main():
    results = {}
    async with BinanceClient() as client:
        # Test 1: Spot klines BTCUSDT 1h
        candles = await client.get_klines("BTCUSDT", "1h", limit=3)
        assert len(candles) > 0
        latest = candles[-1]
        results["btcusdt_1h_klines"] = {
            "count": len(candles),
            "latest_time": latest["time"],
            "open": latest["open"],
            "close": latest["close"],
            "volume": latest["volume"],
        }
        print(f"BINANCE KLINES BTCUSDT 1h: {len(candles)} candles, close={latest['close']}")

        # Test 2: Ticker price
        ticker = await client.get_ticker_price("BTCUSDT")
        results["btcusdt_ticker"] = {"price": ticker["price"]}
        print(f"BINANCE TICKER BTCUSDT: price={ticker['price']}")

        # Test 3: 24hr stats
        stats = await client.get_24hr_stats("ETHUSDT")
        results["ethusdt_24h"] = {
            "lastPrice": stats["lastPrice"],
            "volume": stats["volume"],
        }
        print(f"BINANCE 24H ETHUSDT: price={stats['lastPrice']}, vol={stats['volume']}")

        # Test 4: Futures mark price
        mark = await client.get_futures_mark_price("BTCUSDT")
        results["btcusdt_futures_mark"] = {
            "markPrice": mark["markPrice"],
            "fundingRate": mark["lastFundingRate"],
        }
        print(f"BINANCE FUTURES BTCUSDT: mark={mark['markPrice']}, funding={mark['lastFundingRate']}")

        # Test 5: Futures OI
        oi = await client.get_futures_open_interest("BTCUSDT")
        results["btcusdt_oi"] = {"openInterest": oi["openInterest"]}
        print(f"BINANCE OI BTCUSDT: {oi['openInterest']}")

        # Test 6: OI history
        oih = await client.get_futures_oi_history("BTCUSDT", period="1h", limit=3)
        results["btcusdt_oi_hist"] = {"count": len(oih)}
        print(f"BINANCE OI HIST BTCUSDT: {len(oih)} records")

        # Test 7: Funding rate history
        fr = await client.get_funding_rate_history("BTCUSDT", limit=3)
        results["btcusdt_funding_hist"] = {"count": len(fr)}
        print(f"BINANCE FUNDING HIST BTCUSDT: {len(fr)} records")

        # Test 8: Long/short ratio
        ls = await client.get_long_short_ratio("BTCUSDT", period="1h", limit=1)
        if ls:
            results["btcusdt_ls"] = {"longShortRatio": ls[0]["longShortRatio"]}
            print(f"BINANCE L/S BTCUSDT: ratio={ls[0]['longShortRatio']}")

        # Test 9: ETHUSDT klines
        eth_candles = await client.get_klines("ETHUSDT", "1h", limit=3)
        results["ethusdt_klines"] = {"count": len(eth_candles)}
        print(f"BINANCE KLINES ETHUSDT 1h: {len(eth_candles)} candles")

        # Test 10: SOLUSDT
        sol = await client.get_ticker_price("SOLUSDT")
        results["solusdt_ticker"] = {"price": sol["price"]}
        print(f"BINANCE TICKER SOLUSDT: price={sol['price']}")

    print("\n=== ALL BINANCE TESTS PASSED ===")
    print(f"Timestamp: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}")
    for k, v in results.items():
        print(f"  {k}: {v}")
    return results


if __name__ == "__main__":
    asyncio.run(main())
