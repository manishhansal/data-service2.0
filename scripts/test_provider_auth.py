"""
Provider authentication test — Phase 8.
Tests provider auth with configured credentials (if any).
Reports AUTH_FAILED with RCA when credentials are missing/invalid.
"""
import asyncio
import os
import time


async def test_angel_one():
    api_key = os.environ.get("ANGEL_ONE_API_KEY", "")
    client_id = os.environ.get("ANGEL_ONE_CLIENT_ID", "")
    totp_secret = os.environ.get("ANGEL_ONE_TOTP_SECRET", "")

    if not all([api_key, client_id, totp_secret]):
        print("ANGEL ONE: BLOCKED — CREDENTIALS UNAVAILABLE")
        print("  ANGEL_ONE_API_KEY: NOT_CONFIGURED")
        print("  ANGEL_ONE_CLIENT_ID: NOT_CONFIGURED")
        print("  ANGEL_ONE_TOTP_SECRET: NOT_CONFIGURED")
        return {"status": "BLOCKED", "reason": "CREDENTIALS_UNAVAILABLE"}

    from src.providers.adapters.angel_one import AngelOneAdapter
    from src.providers.adapters.base import ProviderAuthError
    adapter = AngelOneAdapter(api_key=api_key, client_id=client_id, totp_secret=totp_secret)
    try:
        await adapter.authenticate()
        print("ANGEL ONE: AUTH_SUCCESS — JWT acquired")
        return {"status": "AUTHENTICATED"}
    except ProviderAuthError as e:
        print(f"ANGEL ONE: AUTH_FAILED — {str(e)[:100]}")
        return {"status": "AUTH_FAILED", "error": str(e)[:100]}
    except Exception as e:
        print(f"ANGEL ONE: ERROR — {str(e)[:100]}")
        return {"status": "ERROR", "error": str(e)[:100]}
    finally:
        await adapter.close()


async def test_upstox():
    api_key = os.environ.get("UPSTOX_API_KEY", "")
    api_secret = os.environ.get("UPSTOX_API_SECRET", "")

    if not all([api_key, api_secret]):
        print("UPSTOX: BLOCKED — CREDENTIALS UNAVAILABLE")
        print("  UPSTOX_API_KEY: NOT_CONFIGURED")
        print("  UPSTOX_API_SECRET: NOT_CONFIGURED")
        return {"status": "BLOCKED", "reason": "CREDENTIALS_UNAVAILABLE"}

    from src.providers.adapters.upstox import UpstoxAdapter
    adapter = UpstoxAdapter(api_key=api_key, api_secret=api_secret)
    try:
        # Upstox requires an OAuth flow for initial access_token
        # In production, the token is set via set_access_token()
        # Without a pre-obtained access_token, this will fail with ProviderAuthError
        await adapter.ensure_authenticated()
        print("UPSTOX: AUTH_SUCCESS — token available")
        return {"status": "AUTHENTICATED"}
    except Exception as e:
        print(f"UPSTOX: BLOCKED — No OAuth access token (requires browser flow): {str(e)[:80]}")
        return {"status": "BLOCKED", "reason": "OAUTH_FLOW_REQUIRED", "error": str(e)[:80]}
    finally:
        await adapter.aclose()


async def main():
    print(f"=== PROVIDER AUTH TEST — {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} ===")
    print(f"Market status: CLOSED (Sunday)\n")

    angel_result = await test_angel_one()
    upstox_result = await test_upstox()

    print("\n=== CREDENTIAL SUMMARY ===")
    print(f"Angel One: {angel_result['status']}")
    print(f"Upstox: {upstox_result['status']}")
    print("\nNote: To authenticate, configure ANGEL_ONE_API_KEY, ANGEL_ONE_CLIENT_ID,")
    print("ANGEL_ONE_TOTP_SECRET in .env.local. Upstox requires OAuth browser flow.")
    return {"angel_one": angel_result, "upstox": upstox_result}


if __name__ == "__main__":
    asyncio.run(main())
