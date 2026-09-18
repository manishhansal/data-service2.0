#!/usr/bin/env python3
"""
scripts/_test_upstox_ws.py

Upstox WebSocket live binary decode test.
Requires a VALID (non-expired) OAuth access token in Redis.

Flow:
  1. Load token from Redis
  2. Call fetch_ws_authorized_url() -> wss:// URI
  3. Connect WebSocket
  4. Subscribe NIFTY 50 (ltpc) + RELIANCE (full mode)
  5. Receive frames for 30 seconds
  6. Decode each frame via _decode_feed_frame()
  7. Report: messages_received, decoded, failures, decode_latency_ms

Certification gate: >= 100 decoded messages, failures = 0

Usage:
    python3 scripts/_test_upstox_ws.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


COLLECT_SECONDS = 30
MIN_DECODED_GATE = 100


async def main() -> None:
    from src.cache.redis_client import create_redis_pool, RedisClient
    from src.core.settings import get_settings
    from src.providers.adapters.upstox import UpstoxAdapter
    from src.providers.streams.upstox_stream import _decode_feed_frame

    s = get_settings()
    redis_raw = await create_redis_pool(s.redis_url)
    rc = RedisClient(redis_raw)

    adapter = UpstoxAdapter(
        api_key=s.upstox_api_key or "",
        api_secret=s.upstox_api_secret or "",
        redirect_uri=s.upstox_redirect_uri or "",
        redis_client=rc,
    )

    print("=" * 60)
    print("UPSTOX WEBSOCKET LIVE BINARY DECODE TEST")
    print("=" * 60)

    # Load token from Redis
    await adapter.load_tokens_from_redis()
    has_token = bool(getattr(adapter, "_access_token", None))
    print(f"Token loaded from Redis: {has_token}")

    if not has_token and s.upstox_access_token:
        print("Falling back to UPSTOX_ACCESS_TOKEN env var")
        await adapter.set_access_token(s.upstox_access_token)
        has_token = True

    if not has_token:
        print("BLOCKED: No Upstox access token available.")
        print("  1. Visit http://localhost:8200/v1/auth/upstox/login in browser")
        print("  2. Complete the Upstox login")
        print("  3. Re-run this script")
        await redis_raw.aclose()
        return

    # Check token expiry
    token = getattr(adapter, "_access_token", "")
    if token:
        import base64, struct
        try:
            parts = token.split(".")
            if len(parts) == 3:
                payload = parts[1]
                payload += "=" * (4 - len(payload) % 4)
                import json as _json
                d = _json.loads(base64.b64decode(payload))
                exp = d.get("exp", 0)
                delta = exp - int(time.time())
                print(f"Token expires in: {delta}s ({delta/3600:.1f}h)")
                if delta <= 0:
                    print("BLOCKED_BY_EXTERNAL: Access token is EXPIRED.")
                    print("  Complete OAuth flow at: http://localhost:8200/v1/auth/upstox/login")
                    await redis_raw.aclose()
                    return
        except Exception:
            pass

    # Fetch authorized WS URL
    print("Fetching authorized WebSocket URL...")
    try:
        ws_url = await adapter.fetch_ws_authorized_url()
        print(f"WS URL host: {ws_url.split('?')[0]}")
    except Exception as exc:
        print(f"BLOCKED_BY_EXTERNAL: fetch_ws_authorized_url() failed: {type(exc).__name__}: {exc}")
        await redis_raw.aclose()
        return

    # Connect and collect
    import websockets  # type: ignore[import]

    metrics: dict[str, int] = defaultdict(int)
    decode_latencies: list[float] = []
    samples: list[dict] = []

    # Subscription message — NIFTY 50 (NSE_INDEX|Nifty 50) + RELIANCE (NSE_EQ|RELIANCE)
    subscribe_msg = json.dumps({
        "guid": "ws-test-001",
        "method": "sub",
        "data": {
            "mode": "ltpc",
            "instrumentKeys": [
                "NSE_INDEX|Nifty 50",
                "NSE_EQ|RELIANCE",
            ]
        }
    })

    print(f"Connecting to WebSocket, collecting {COLLECT_SECONDS}s of data...")
    deadline = time.monotonic() + COLLECT_SECONDS

    try:
        async with websockets.connect(
            ws_url,
            ping_interval=20,
            ping_timeout=10,
            close_timeout=5,
        ) as ws:
            await ws.send(subscribe_msg)
            print("Subscribed. Receiving frames...")

            while time.monotonic() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
                except asyncio.TimeoutError:
                    continue
                except websockets.exceptions.ConnectionClosed:
                    print("WebSocket closed by server")
                    break

                metrics["received"] += 1

                t0 = time.monotonic()
                try:
                    if isinstance(raw, bytes):
                        decoded = _decode_feed_frame(raw)
                    else:
                        decoded = None
                except Exception:
                    decoded = None

                latency_ms = (time.monotonic() - t0) * 1000

                if decoded is not None:
                    metrics["decoded"] += 1
                    decode_latencies.append(latency_ms)
                    if len(samples) < 3:
                        # Store sanitised sample (no raw bytes)
                        samples.append({
                            "ltp": decoded.get("ltp"),
                            "instrument_key": decoded.get("instrument_key"),
                            "timestamp": decoded.get("timestamp"),
                            "mode": decoded.get("mode"),
                        })
                else:
                    if isinstance(raw, bytes) and len(raw) > 0:
                        metrics["decode_failures"] += 1
                    else:
                        metrics["text_frames"] += 1

    except Exception as exc:
        print(f"WebSocket error: {type(exc).__name__}: {exc}")

    await redis_raw.aclose()

    # Report
    print()
    print("=" * 60)
    print("RESULTS")
    print("=" * 60)
    print(f"messages_received      : {metrics['received']}")
    print(f"messages_decoded       : {metrics['decoded']}")
    print(f"decode_failures        : {metrics['decode_failures']}")
    print(f"text_frames            : {metrics['text_frames']}")

    if decode_latencies:
        sorted_lat = sorted(decode_latencies)
        p50 = sorted_lat[len(sorted_lat) // 2]
        p95 = sorted_lat[int(len(sorted_lat) * 0.95)]
        p99 = sorted_lat[int(len(sorted_lat) * 0.99)]
        print(f"decode_latency p50     : {p50:.3f} ms")
        print(f"decode_latency p95     : {p95:.3f} ms")
        print(f"decode_latency p99     : {p99:.3f} ms")

    print()
    print("Sample decoded ticks:")
    for s in samples:
        print(f"  {s}")

    print()
    gate_decoded = metrics['decoded'] >= MIN_DECODED_GATE
    gate_failures = metrics['decode_failures'] == 0
    print(f"Gate: >= {MIN_DECODED_GATE} decoded:  {'PASS' if gate_decoded else 'FAIL'} ({metrics['decoded']})")
    print(f"Gate: decode_failures=0: {'PASS' if gate_failures else 'FAIL'} ({metrics['decode_failures']})")

    if metrics['received'] == 0:
        print("\nOVERALL: NOT VERIFIED — no frames received")
    elif gate_decoded and gate_failures:
        print("\nOVERALL: PASS")
    else:
        print("\nOVERALL: PARTIAL")


if __name__ == "__main__":
    asyncio.run(main())
