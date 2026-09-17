#!/usr/bin/env python3
"""Check Upstox OAuth token status."""
import base64
import json
import os
import time

env = {}
with open(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env.local")) as fh:
    for line in fh:
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            env[k] = v

access_token = env.get("UPSTOX_ACCESS_TOKEN", "")
print("UPSTOX_ACCESS_TOKEN present:", bool(access_token))
print("UPSTOX_CLIENT_ID present:", bool(env.get("UPSTOX_CLIENT_ID")))
print("UPSTOX_CLIENT_SECRET present:", bool(env.get("UPSTOX_CLIENT_SECRET")))
print("UPSTOX_REDIRECT_URI present:", bool(env.get("UPSTOX_REDIRECT_URI")))

if access_token:
    try:
        parts = access_token.split(".")
        if len(parts) == 3:
            payload = json.loads(base64.b64decode(parts[1] + "=="))
            exp = payload.get("exp", 0)
            now = time.time()
            delta = int(exp - now)
            status = "EXPIRED" if exp < now else "VALID"
            print(f"Token expiry: {status}, delta={delta}s")
        else:
            print("Token is opaque (non-JWT)")
    except Exception as exc:
        print(f"Token parse error: {exc}")
else:
    print("Token: NOT CONFIGURED")
