"""
Deribit live test — Phase 11.
Tests real Deribit data acquisition via DATA-SERVICE adapter.
"""
import asyncio
import time
from src.providers.deribit_client import DeribitClient


async def main():
    results = {}
    async with DeribitClient() as client:
        # Test 1: Index prices
        for currency, index_name in [("BTC", "btc_usd"), ("ETH", "eth_usd")]:
            idx = await client.get_index_price(index_name)
            results[f"{currency}_index"] = {"index_price": idx.get("index_price"), "index_name": idx.get("index_name")}
            print(f"DERIBIT {currency} index price: {idx.get('index_price')}")

        # Test 2: Get instruments (options)
        btc_options = await client.get_instruments("BTC", kind="option")
        results["btc_options_count"] = len(btc_options)
        print(f"DERIBIT BTC options: {len(btc_options)} instruments")

        # Test 3: Get instruments (futures)
        btc_futures = await client.get_instruments("BTC", kind="future")
        results["btc_futures_count"] = len(btc_futures)
        print(f"DERIBIT BTC futures: {len(btc_futures)} instruments")

        # Test 4: Get ticker for BTC-PERPETUAL
        try:
            ticker = await client.get_ticker("BTC-PERPETUAL")
            results["btc_perpetual"] = {
                "mark_price": ticker.get("mark_price"),
                "last_price": ticker.get("last_price"),
                "open_interest": ticker.get("open_interest"),
            }
            print(f"DERIBIT BTC-PERPETUAL: mark={ticker.get('mark_price')}, OI={ticker.get('open_interest')}")
        except Exception as e:
            results["btc_perpetual"] = {"error": str(e)[:80]}
            print(f"DERIBIT BTC-PERPETUAL: {str(e)[:80]}")

        # Test 5: Get OHLCV for BTC-PERPETUAL
        import time as t
        end_ts = int(t.time() * 1000)
        start_ts = end_ts - 3600_000  # 1 hour back
        try:
            ohlcv = await client.get_ohlcv("BTC-PERPETUAL", "60", start_ts, end_ts)
            results["btc_perp_ohlcv"] = {"count": len(ohlcv), "latest_close": ohlcv[-1]["close"] if ohlcv else None}
            print(f"DERIBIT BTC-PERPETUAL 1h OHLCV: {len(ohlcv)} candles")
        except Exception as e:
            results["btc_perp_ohlcv"] = {"error": str(e)[:80]}
            print(f"DERIBIT OHLCV error: {str(e)[:80]}")

        # Test 6: Get order book for a near-ATM option
        if btc_options:
            near_expiry = sorted(btc_options, key=lambda x: x.get("expiration_timestamp", float("inf")))[0]
            instrument_name = near_expiry.get("instrument_name", "")
            try:
                book = await client.get_order_book(instrument_name)
                results["btc_option_book"] = {
                    "instrument": instrument_name,
                    "mark_price": book.get("mark_price"),
                    "mark_iv": book.get("mark_iv"),  # null is correct when not available
                    "open_interest": book.get("open_interest"),
                }
                print(f"DERIBIT order book {instrument_name}: mark={book.get('mark_price')}, IV={book.get('mark_iv')}")
            except Exception as e:
                results["btc_option_book"] = {"error": str(e)[:80]}

    print(f"\n=== DERIBIT TESTS COMPLETE ===")
    print(f"Timestamp: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}")
    for k, v in results.items():
        print(f"  {k}: {v}")
    return results


if __name__ == "__main__":
    asyncio.run(main())
