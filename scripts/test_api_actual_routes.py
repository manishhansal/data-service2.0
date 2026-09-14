"""
API test against actual registered routes — Phase 14.
Tests the real endpoints that exist vs what the docs claim.
"""
import requests
import time

BASE = "http://localhost:8200"
HDR = {"X-API-Key": "audit-key-1"}

tests = [
    ("GET", "/v1/health/live", None, "Health: live"),
    ("GET", "/v1/health/ready", None, "Health: ready (503 acceptable)"),
    ("GET", "/v1/health/data", 200, "Health: data"),
    ("GET", "/metrics", 200, "Prometheus metrics"),
    ("GET", "/v1/auth/token?api_key=audit-key-1", 200, "Auth: token"),
    ("GET", "/v1/instruments?exchange=NSE", 200, "Instruments: list NSE"),
    ("GET", "/v1/instruments/fno-universe", 200, "Instruments: F&O universe"),
    ("GET", "/v1/india/quotes/NIFTY", 200, "India: quote NIFTY"),
    ("GET", "/v1/india/quotes/BANKNIFTY", 200, "India: quote BANKNIFTY"),
    ("GET", "/v1/india/quotes/RELIANCE", 200, "India: quote RELIANCE"),
    ("GET", "/v1/india/market/status", 200, "India: market status"),
    ("GET", "/v1/india/historical?symbol=RELIANCE&exchange=NSE&interval=1d&from=2024-01-10&to=2024-01-15", 200, "India: RELIANCE 1d hist"),
    ("GET", "/v1/india/historical?symbol=NIFTY&exchange=NSE&interval=1d&from=2024-01-10&to=2024-01-15", 200, "India: NIFTY 1d hist"),
    ("GET", "/v1/india/historical?symbol=RELIANCE&exchange=NSE&interval=3m&from=2024-01-10&to=2024-01-15", 400, "India: 3m REJECTED"),
    ("GET", "/v1/india/historical/status", 200, "India: hist status"),
    ("GET", "/v1/india/historical/gaps", 200, "India: hist gaps"),
    ("GET", "/v1/india/historical/reconciliation", 200, "India: reconciliation"),
    ("GET", "/v1/india/option-chain?underlying=NIFTY", 200, "India: option chain NIFTY"),
    ("GET", "/v1/crypto/BTCUSDT/ohlcv?interval=1h&limit=3", 200, "Crypto: BTCUSDT 1h klines"),
    ("GET", "/v1/crypto/ETHUSDT/ohlcv?interval=1h&limit=3", 200, "Crypto: ETHUSDT 1h klines"),
    ("GET", "/v1/crypto/SOLUSDT/ohlcv?interval=1h&limit=3", 200, "Crypto: SOLUSDT 1h klines"),
    ("GET", "/v1/crypto/BTCUSDT/ticker", 200, "Crypto: BTCUSDT ticker"),
    ("GET", "/v1/crypto/ETHUSDT/ticker", 200, "Crypto: ETHUSDT ticker"),
    ("GET", "/v1/crypto/BTCUSDT/stats", 200, "Crypto: BTCUSDT 24h stats"),
    ("GET", "/v1/crypto/futures/overview", 200, "Crypto: futures overview"),
    ("GET", "/v1/deribit/BTC/overview", 200, "Deribit: BTC overview"),
    ("GET", "/v1/deribit/ETH/overview", 200, "Deribit: ETH overview"),
    ("GET", "/v1/deribit/SOL/overview", 200, "Deribit: SOL overview"),
    ("GET", "/v1/deribit/XRP/overview", 400, "Deribit: XRP unsupported → 400"),
    ("GET", "/v1/deribit/BTC/index-price", 200, "Deribit: BTC index price"),
    ("GET", "/v1/deribit/BTC/instruments?kind=future", 200, "Deribit: BTC futures list"),
    ("GET", "/v1/providers/health", 200, "Providers: health"),
    ("GET", "/v1/stream/status", 200, "Stream: status"),
    ("GET", "/v1/analytics/health", 200, "Analytics: health"),
    ("GET", "/v1/provenance", 200, "Provenance: list"),
    ("POST", "/v1/quality/score", 200, "Quality: score POST"),
    ("POST", "/v1/quality/evaluate", 200, "Quality: evaluate POST"),
]

QUALITY_PAYLOAD = {
    "symbol": "NIFTY",
    "completenessPercent": 95.0,
    "providerHealthy": True,
    "timestampValid": True,
    "semanticallyValid": True,
    "freshnessStatus": "LIVE",
}

QUALITY_EVAL_PAYLOAD = {
    "symbol": "NIFTY",
    "quoteAgeMs": 1000,
    "completenessPercent": 95.0,
    "providerHealthy": True,
    "timestampValid": True,
    "semanticallyValid": True,
}

passed = failed = 0
results = {}

print(f"=== API ACTUAL ROUTES TEST — {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} ===\n")

for method, path, expected, desc in tests:
    try:
        if method == "POST" and "quality/score" in path:
            r = requests.post(BASE + path, headers=HDR, json=QUALITY_PAYLOAD, timeout=10)
        elif method == "POST" and "quality/evaluate" in path:
            r = requests.post(BASE + path, headers=HDR, json=QUALITY_EVAL_PAYLOAD, timeout=10)
        else:
            r = requests.get(BASE + path, headers=HDR, timeout=10)

        actual_status = r.status_code
        ok = (expected is None or actual_status == expected)
        label = "PASS" if ok else f"FAIL(got {actual_status})"
        if ok:
            passed += 1
        else:
            failed += 1
        print(f"  [{label}] {method} {path}")
        results[desc] = {"status": actual_status, "passed": ok}

        # Print key data for important responses
        if ok and actual_status == 200:
            try:
                d = r.json()
                data = d.get("data", d)
                if "BTCUSDT/ohlcv" in path and isinstance(data, list):
                    latest = data[-1]
                    print(f"         candles={len(data)}, latest_close={latest.get('close')}")
                elif "ticker" in path:
                    price = data.get("price") if isinstance(data, dict) else None
                    print(f"         price={price}")
                elif "futures/overview" in path and isinstance(data, list):
                    sym = data[0].get("symbol") if data else None
                    mark = data[0].get("markPrice") if data else None
                    print(f"         {sym}: markPrice={mark}")
                elif "deribit" in path and "overview" in path:
                    idx = data.get("indexPrice") if isinstance(data, dict) else None
                    tc = data.get("totalContracts") if isinstance(data, dict) else None
                    print(f"         indexPrice={idx}, totalContracts={tc}")
                elif "market/status" in path:
                    phase = data.get("sessionPhase") if isinstance(data, dict) else None
                    print(f"         sessionPhase={phase}")
                elif "3m" in path and actual_status == 400:
                    err = d.get("error", {}).get("code")
                    print(f"         error_code={err} ✓ (3m correctly rejected)")
            except Exception:
                pass
    except Exception as e:
        failed += 1
        print(f"  [ERROR] {method} {path}: {str(e)[:60]}")
        results[desc] = {"status": "ERROR", "passed": False}

print(f"\n=== RESULTS: {passed}/{passed+failed} endpoints passed ===")
print(f"Timestamp: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}")
