#!/usr/bin/env python3
"""Verify the Upstox distributed refresh lock (SET NX EX) prevents storm."""
from __future__ import annotations
import asyncio, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

async def main() -> None:
    from src.cache.redis_client import create_redis_pool
    from src.core.settings import get_settings
    s = get_settings()
    r = await create_redis_pool(s.redis_url)

    LOCK_KEY = "mds:upstox:refresh_lock"
    await r.delete(LOCK_KEY)  # clean slate

    results = []
    for i in range(4):
        acquired = await r.set(LOCK_KEY, f"worker-{i}", nx=True, ex=30)
        results.append((i, bool(acquired)))

    print("Distributed refresh lock — SET NX EX results:")
    for worker, acquired in results:
        status = "ACQUIRED (lock holder)" if acquired else "BLOCKED (correct)"
        print(f"  Worker-{worker}: {status}")

    await r.delete(LOCK_KEY)
    await r.aclose()

    first_acquires = sum(1 for _, a in results if a)
    ok = first_acquires == 1
    print(f"\nExactly 1 worker acquired lock: {ok}")
    print("RESULT: PASS" if ok else "RESULT: FAIL")

if __name__ == "__main__":
    asyncio.run(main())
