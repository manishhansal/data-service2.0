#!/bin/bash
# API endpoint test using curl — avoids requests library chunked encoding issues
BASE="http://localhost:8200"
API_KEY="audit-key-1"
PASS=0
FAIL=0

check() {
    local desc="$1"
    local method="$2"
    local path="$3"
    local expected_status="$4"
    local extra_args="${5:-}"
    
    actual_status=$(curl -s --max-time 10 -o /tmp/api_resp.json -w "%{http_code}" \
        -H "X-API-Key: $API_KEY" \
        -X "$method" \
        $extra_args \
        "$BASE$path" 2>&1)
    
    if [ "$expected_status" = "ANY" ] || [ "$actual_status" = "$expected_status" ]; then
        echo "  [PASS] $desc → HTTP $actual_status"
        PASS=$((PASS + 1))
    else
        echo "  [FAIL] $desc → expected $expected_status, got $actual_status"
        cat /tmp/api_resp.json | head -c 200
        echo
        FAIL=$((FAIL + 1))
    fi
}

echo "=== DATA-SERVICE 2.0 API TEST ==="
echo "Timestamp: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo

echo "--- Health (no auth) ---"
check "health/live" GET /v1/health/live 200
check "health/ready (503 ok)" GET /v1/health/ready ANY
check "health/data" GET /v1/health/data 200
check "metrics" GET /metrics 200

echo
echo "--- Auth ---"
check "auth/token" GET "/v1/auth/token?api_key=$API_KEY" 200

echo
echo "--- Instruments ---"
check "instruments NSE list" GET "/v1/instruments?exchange=NSE" 200
check "instruments F&O universe" GET "/v1/instruments/fno-universe" 200

echo
echo "--- India Market ---"
check "india quotes NIFTY" GET "/v1/india/quotes/NIFTY" 200
check "india quotes BANKNIFTY" GET "/v1/india/quotes/BANKNIFTY" 200
check "india quotes RELIANCE" GET "/v1/india/quotes/RELIANCE" 200
check "india market status" GET "/v1/india/market/status" 200
check "india historical RELIANCE 1d" GET "/v1/india/historical?symbol=RELIANCE&exchange=NSE&interval=1d&from=2024-01-10&to=2024-01-15" 200
check "india historical NIFTY 1d" GET "/v1/india/historical?symbol=NIFTY&exchange=NSE&interval=1d&from=2024-01-10&to=2024-01-15" 200
check "india 3m REJECTED" GET "/v1/india/historical?symbol=RELIANCE&exchange=NSE&interval=3m&from=2024-01-10&to=2024-01-15" 400
check "india hist status" GET "/v1/india/historical/status" 200
check "india hist gaps" GET "/v1/india/historical/gaps" 200
check "india hist reconciliation" GET "/v1/india/historical/reconciliation" 200
check "india option chain NIFTY" GET "/v1/india/option-chain?underlying=NIFTY" 200

echo
echo "--- Crypto ---"
check "crypto BTCUSDT 1h klines" GET "/v1/crypto/BTCUSDT/ohlcv?interval=1h&limit=3" 200
check "crypto ETHUSDT 1h klines" GET "/v1/crypto/ETHUSDT/ohlcv?interval=1h&limit=3" 200
check "crypto SOLUSDT 1h klines" GET "/v1/crypto/SOLUSDT/ohlcv?interval=1h&limit=3" 200
check "crypto BTCUSDT 3m klines (allowed for crypto)" GET "/v1/crypto/BTCUSDT/ohlcv?interval=3m&limit=3" 200
check "crypto BTCUSDT ticker" GET "/v1/crypto/BTCUSDT/ticker" 200
check "crypto ETHUSDT ticker" GET "/v1/crypto/ETHUSDT/ticker" 200
check "crypto BTCUSDT 24h stats" GET "/v1/crypto/BTCUSDT/stats" 200
check "crypto futures overview" GET "/v1/crypto/futures/overview" 200

echo
echo "--- Deribit Options ---"
check "deribit BTC overview" GET "/v1/deribit/BTC/overview" 200
check "deribit ETH overview" GET "/v1/deribit/ETH/overview" 200
check "deribit SOL overview" GET "/v1/deribit/SOL/overview" 200
check "deribit XRP unsupported → 400" GET "/v1/deribit/XRP/overview" 400
check "deribit BTC index price" GET "/v1/deribit/BTC/index-price" 200
check "deribit BTC futures" GET "/v1/deribit/BTC/instruments?kind=future" 200
check "deribit BTC options" GET "/v1/deribit/BTC/instruments?kind=option" 200

echo
echo "--- Observability ---"
check "providers health" GET "/v1/providers/health" 200
check "stream status" GET "/v1/stream/status" 200
check "analytics health" GET "/v1/analytics/health" 200

echo
echo "=== SUMMARY ==="
echo "PASS: $PASS / FAIL: $FAIL"
echo "Total: $((PASS + FAIL))"
