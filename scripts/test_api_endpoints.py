"""
API endpoint test — Phase 14.
Tests all documented DATA-SERVICE 2.0 endpoints against the running service.
"""
import asyncio
import httpx
import time

BASE_URL = "http://localhost:8200"
API_KEY = "audit-key-1"
HEADERS = {"X-API-Key": API_KEY}
RESULTS = {}


async def check(client, name, method, path, expected_status, params=None, json=None, skip_auth=False):
    headers = {} if skip_auth else HEADERS
    try:
        r = await client.request(method, f"{BASE_URL}{path}", params=params, json=json, headers=headers)
        passed = r.status_code == expected_status
        status_str = "PASS" if passed else f"FAIL (got {r.status_code})"
        RESULTS[name] = {"status": r.status_code, "passed": passed, "path": path}
        snippet = ""
        if not passed:
            try:
                snippet = str(r.json())[:120]
            except Exception:
                snippet = r.text[:120]
        print(f"  [{status_str}] {method} {path} → {r.status_code}{f' | {snippet}' if snippet else ''}")
        return r
    except Exception as e:
        RESULTS[name] = {"status": "ERROR", "passed": False, "error": str(e)[:80], "path": path}
        print(f"  [ERROR] {method} {path} → {str(e)[:80]}")
        return None


async def main():
    print(f"=== DATA-SERVICE API TEST — {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} ===\n")

    async with httpx.AsyncClient(timeout=15) as client:
        print("--- Health endpoints (no auth) ---")
        await check(client, "health_live", "GET", "/v1/health/live", 200, skip_auth=True)
        await check(client, "health_ready", "GET", "/v1/health/ready", 200, skip_auth=True)
        await check(client, "health_data", "GET", "/v1/health/data", 200, skip_auth=True)
        await check(client, "metrics", "GET", "/metrics", 200, skip_auth=True)

        print("\n--- Auth endpoint ---")
        r = await check(client, "auth_token", "GET", "/v1/auth/token", 200,
                       params={"api_key": API_KEY}, skip_auth=True)
        jwt_token = None
        if r and r.status_code == 200:
            try:
                jwt_token = r.json().get("access_token")
                print(f"    JWT acquired: {'YES' if jwt_token else 'NO'}")
            except Exception:
                pass

        print("\n--- Instruments endpoints ---")
        await check(client, "instruments_list", "GET", "/v1/instruments", 200,
                   params={"exchange": "NSE"})
        await check(client, "instruments_fno", "GET", "/v1/instruments/fno-universe", 200)
        # 404 test for unknown instrument
        await check(client, "instrument_404", "GET", "/v1/instruments/nonexistent_instrument_id_xyz", 404)

        print("\n--- India market endpoints ---")
        await check(client, "india_quotes_nifty", "GET", "/v1/india/quotes/NIFTY", 200)
        await check(client, "india_quotes_batch", "GET", "/v1/india/quotes",
                   params={"symbols": "NIFTY,BANKNIFTY"}, expected_status=200)
        await check(client, "india_historical_reliance", "GET", "/v1/india/historical",
                   params={"symbol": "RELIANCE", "exchange": "NSE", "interval": "1d",
                           "from": "2024-01-10", "to": "2024-01-15"}, expected_status=200)
        # Verify 3m is rejected
        await check(client, "india_3m_rejected", "GET", "/v1/india/historical",
                   params={"symbol": "RELIANCE", "exchange": "NSE", "interval": "3m",
                           "from": "2024-01-10", "to": "2024-01-15"}, expected_status=400)
        await check(client, "india_market_status", "GET", "/v1/india/market/status", 200)
        await check(client, "india_option_chain_nifty", "GET", "/v1/india/option-chain",
                   params={"underlying": "NIFTY"}, expected_status=200)
        await check(client, "india_historical_status", "GET", "/v1/india/historical/status", 200)
        await check(client, "india_historical_gaps", "GET", "/v1/india/historical/gaps", 200)

        print("\n--- Broker analytics endpoints ---")
        await check(client, "broker_pcr", "GET", "/v1/india/broker-analytics/pcr", 200)
        await check(client, "broker_oi_buildup", "GET", "/v1/india/broker-analytics/oi-buildup", 200)
        await check(client, "broker_gainers_losers", "GET", "/v1/india/broker-analytics/gainers-losers", 200)

        print("\n--- Crypto endpoints ---")
        r_btc = await check(client, "crypto_klines_btcusdt", "GET", "/v1/crypto/klines/BTCUSDT",
                           params={"interval": "1h"}, expected_status=200)
        if r_btc and r_btc.status_code == 200:
            data = r_btc.json()
            print(f"    Crypto klines: {len(data.get('data', []))} candles returned")

        await check(client, "crypto_futures_overview", "GET", "/v1/crypto/futures/overview", 200)
        await check(client, "crypto_futures_funding", "GET", "/v1/crypto/futures/funding-history/BTCUSDT", 200)
        await check(client, "crypto_futures_oi_hist", "GET", "/v1/crypto/futures/oi-history/BTCUSDT", 200)
        await check(client, "crypto_futures_ls", "GET", "/v1/crypto/futures/long-short/BTCUSDT", 200)

        print("\n--- Deribit options endpoints ---")
        await check(client, "deribit_btc_overview", "GET", "/v1/crypto/options/BTC/overview", 200)
        await check(client, "deribit_eth_overview", "GET", "/v1/crypto/options/ETH/overview", 200)
        await check(client, "deribit_invalid_currency", "GET", "/v1/crypto/options/XRP/overview", 400)

        print("\n--- Quality endpoints ---")
        await check(client, "quality_gate_post", "POST", "/v1/quality/gate", 200,
                   json={"symbol": "NIFTY", "quoteAgeMs": 1000, "completenessPercent": 95,
                         "providerHealthy": True, "timestampValid": True, "semanticallyValid": True})

        print("\n--- Provider health ---")
        await check(client, "providers_health", "GET", "/v1/providers/health", 200)

        print("\n--- Stream status ---")
        await check(client, "stream_status", "GET", "/v1/stream/status", 200)

        print("\n--- Parity contract ---")
        await check(client, "parity_contract", "GET", "/v1/parity/contract", 200)

    passed = sum(1 for v in RESULTS.values() if v.get("passed"))
    total = len(RESULTS)
    print(f"\n=== RESULTS: {passed}/{total} endpoints passed ===")

    print("\n--- Failed endpoints ---")
    for k, v in RESULTS.items():
        if not v.get("passed"):
            print(f"  FAIL: {k} → {v}")

    return RESULTS


if __name__ == "__main__":
    asyncio.run(main())
