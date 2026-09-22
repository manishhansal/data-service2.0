#!/usr/bin/env python3
"""
Verify Upstox multi-worker Redis token sharing.
Stores access token in Redis, then confirms UpstoxAdapter.load_tokens_from_redis()
picks it up.  Token value is never printed.
"""
from __future__ import annotations
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


async def main() -> None:
    from src.cache.redis_client import create_redis_pool, RedisClient
    from src.core.settings import get_settings

    s = get_settings()
    redis_raw = await create_redis_pool(s.redis_url)

    TOKEN_KEY = "mds:upstox:access_token"  # matches UpstoxAdapter._REDIS_TOKEN_KEY

    token = s.upstox_access_token or ""
    if not token:
        print("SKIP: No UPSTOX_ACCESS_TOKEN in environment — can't test storage")
        return

    # Store the token (may be expired — we're testing the code path, not liveness)
    await redis_raw.set(TOKEN_KEY, token, ex=7200)

    ttl = await redis_raw.ttl(TOKEN_KEY)
    stored_val = await redis_raw.get(TOKEN_KEY)
    assert stored_val is not None, "Token not found after SET"
    print(f"[1] Token stored in Redis: len={len(stored_val)} chars, TTL={ttl}s")

    # Simulate a second worker loading from Redis
    from src.providers.adapters.upstox import UpstoxAdapter

    rc = RedisClient(redis_raw)
    adapter = UpstoxAdapter(
        api_key=s.upstox_api_key or "",
        api_secret=s.upstox_api_secret or "",
        redirect_uri=s.upstox_redirect_uri or "",
        redis_client=rc,
    )

    await adapter.load_tokens_from_redis()

    # Check loaded state without printing token values
    store = getattr(adapter, "_token_store", None)
    if store is not None:
        loaded = store.access_token is not None
        print(f"[2] UpstoxAdapter.load_tokens_from_redis(): access_token loaded = {loaded}")
    else:
        # Fallback: check _access_token directly
        loaded = bool(getattr(adapter, "_access_token", None))
        print(f"[2] UpstoxAdapter._access_token loaded = {loaded}")

    # Simulate a third worker — same result expected
    adapter2 = UpstoxAdapter(
        api_key=s.upstox_api_key or "",
        api_secret=s.upstox_api_secret or "",
        redirect_uri=s.upstox_redirect_uri or "",
        redis_client=rc,
    )
    await adapter2.load_tokens_from_redis()
    store2 = getattr(adapter2, "_token_store", None)
    loaded2 = (store2.access_token is not None) if store2 else bool(getattr(adapter2, "_access_token", None))
    print(f"[3] Worker-2 load_tokens_from_redis(): access_token loaded = {loaded2}")

    # Both workers should have the same token (same Redis value)
    tok1 = (store.access_token if store else getattr(adapter, "_access_token", ""))
    tok2 = (store2.access_token if store2 else getattr(adapter2, "_access_token", ""))
    tokens_match = tok1 == tok2
    print(f"[4] Worker-1 and Worker-2 tokens match: {tokens_match}")

    # Clean up
    await adapter.aclose()
    await adapter2.aclose()
    await redis_raw.aclose()

    if loaded and loaded2 and tokens_match:
        print("\nRESULT: PASS — multi-worker Redis token sharing verified")
    else:
        print("\nRESULT: FAIL — check above")


if __name__ == "__main__":
    asyncio.run(main())
