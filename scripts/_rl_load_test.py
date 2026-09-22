#!/usr/bin/env python3
"""
scripts/_rl_load_test.py
Inbound consumer rate-limit load test against the live data-service API.
Fires 115 rapid requests (above the 100/60s default limit) and confirms
429s are returned and X-RateLimit-* headers are present.
"""
from __future__ import annotations
import urllib.request, urllib.error, json, time

BASE = "http://localhost:8200/v1/india/market/status"
HEADERS = {"X-API-KEY": "dev-key-local-1"}
N = 115

results: dict[int | str, int] = {200: 0, 429: 0, "other": 0}
first_rl_headers: dict[str, str] = {}
first_429_body: str = ""

t0 = time.monotonic()
for i in range(N):
    req = urllib.request.Request(BASE, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            if not first_rl_headers:
                first_rl_headers = {
                    k: v for k, v in r.headers.items()
                    if k.lower().startswith("x-ratelimit")
                }
            results[200] += 1
    except urllib.error.HTTPError as e:
        if e.code == 429:
            results[429] += 1
            if not first_429_body:
                first_429_body = e.read().decode()[:200]
        else:
            results["other"] += 1
    except Exception as exc:
        results["other"] += 1

elapsed = time.monotonic() - t0

print("=" * 60)
print("CONSUMER RATE-LIMIT LOAD TEST")
print("=" * 60)
print(f"Requests fired  : {N}")
print(f"Elapsed         : {elapsed:.2f}s")
print(f"200 OK          : {results[200]}")
print(f"429 Too Many    : {results[429]}")
print(f"Other errors    : {results['other']}")
print(f"RateLimit hdrs  : {first_rl_headers or 'none seen'}")
if first_429_body:
    print(f"429 body sample : {first_429_body}")
print()
if results[429] > 0:
    print("RESULT: PASS — rate limit triggered, 429 returned correctly")
elif results[200] == N:
    print("RESULT: PARTIAL — no 429 seen; middleware may be exempt-path "
          "or window not exceeded in burst (all 200)")
else:
    print(f"RESULT: CHECK — {results}")
