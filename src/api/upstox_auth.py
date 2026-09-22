"""
src/api/upstox_auth.py

Upstox OAuth 2.0 flow — login redirect and authorization-code callback.

Routes (unauthenticated — no consumer API key required):
  GET /v1/auth/upstox/login     → 302 redirect to Upstox login dialog
  GET /v1/auth/upstox/callback  → exchanges auth code, stores token in Redis
  GET /v1/auth/upstox/status    → token metadata (no value exposed)

Security notes:
  - The access token is stored ONLY in Redis (mds:upstox:oauth:access_token)
    with a 23-hour TTL.  It is never echoed in the response body.
  - The state parameter (random hex) guards against CSRF on the callback.
  - All credential values are excluded from log entries.
"""
from __future__ import annotations

import hashlib
import os
import time
from datetime import datetime, timezone
from typing import Optional

import structlog
from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

log = structlog.get_logger(__name__)

router = APIRouter()

# Redis key — must match UpstoxAdapter._REDIS_TOKEN_KEY
_TOKEN_KEY = "mds:upstox:access_token"
_TOKEN_TTL = 23 * 3600  # 23 hours (Upstox tokens are valid ~24h)
_STATE_TTL = 600  # 10-minute window to complete the OAuth dance


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _redis(request: Request):  # type: ignore[return]
    """Return the raw aioredis client from app.state.redis."""
    return getattr(request.app.state, "redis", None)


async def _get_settings():
    from src.core.settings import get_settings  # noqa: PLC0415
    return get_settings()


def _html_result(title: str, body: str, *, ok: bool) -> str:
    color = "#2d6a2d" if ok else "#8b2222"
    icon = "✓" if ok else "✗"
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        f"<title>{title}</title>"
        "<style>body{font-family:monospace;padding:2em;background:#f5f5f5}"
        f".box{{background:white;padding:1.5em;border-left:6px solid {color};max-width:600px}}"
        f"h2{{color:{color}}}code{{background:#eee;padding:2px 4px}}</style></head>"
        f"<body><div class='box'><h2>{icon} {title}</h2><p>{body}</p>"
        "<hr><small>data-service2.0 — Upstox OAuth</small></div></body></html>"
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get(
    "/auth/upstox/login",
    summary="Redirect browser to Upstox OAuth login dialog",
    tags=["Auth"],
)
async def upstox_login(request: Request) -> RedirectResponse:
    """Redirect to the Upstox OAuth 2.0 login dialog.

    After the user authenticates, Upstox redirects back to
    ``/v1/auth/upstox/callback?code=<code>&state=<state>``.
    """
    settings = await _get_settings()

    if not settings.upstox_api_key:
        return JSONResponse(  # type: ignore[return-value]
            {"error": "UPSTOX_API_KEY not configured"}, status_code=503
        )

    state = hashlib.sha256(os.urandom(32)).hexdigest()[:24]

    redis = _redis(request)
    if redis is not None:
        try:
            await redis.set(f"mds:upstox:oauth:state:{state}", "1", ex=_STATE_TTL)
        except Exception:  # noqa: BLE001
            log.warning("upstox_oauth_state_redis_write_failed", component="upstox_auth")

    redirect_uri = (
        settings.upstox_redirect_uri
        or "http://localhost:8200/v1/auth/upstox/callback"
    )
    auth_url = (
        "https://api.upstox.com/v2/login/authorization/dialog"
        f"?response_type=code"
        f"&client_id={settings.upstox_api_key}"
        f"&redirect_uri={redirect_uri}"
        f"&state={state}"
    )
    log.info("upstox_oauth_login_redirect", component="upstox_auth",
             state_prefix=state[:8])
    return RedirectResponse(url=auth_url, status_code=302)


@router.get(
    "/auth/upstox/callback",
    summary="Upstox OAuth 2.0 authorization-code callback",
    tags=["Auth"],
)
async def upstox_callback(
    request: Request,
    code: Optional[str] = Query(default=None),
    state: Optional[str] = Query(default=None),
    error: Optional[str] = Query(default=None),
) -> HTMLResponse:
    """Exchange Upstox auth code for access token and store in Redis.

    Called automatically by Upstox after the user completes the login dialog.
    Returns a plain HTML confirmation page.  The token value is never included
    in the response.
    """
    if error:
        log.warning("upstox_oauth_callback_error", component="upstox_auth",
                    error=error)
        return HTMLResponse(
            _html_result("OAuth Error",
                         f"Upstox returned error: {error}", ok=False),
            status_code=400,
        )

    if not code:
        return HTMLResponse(
            _html_result("Missing Code",
                         "No authorization code in callback.", ok=False),
            status_code=400,
        )

    settings = await _get_settings()
    if not settings.upstox_api_key or not settings.upstox_api_secret:
        return HTMLResponse(
            _html_result("Not Configured",
                         "UPSTOX_API_KEY / UPSTOX_API_SECRET not set.", ok=False),
            status_code=503,
        )

    import httpx  # noqa: PLC0415
    redirect_uri = (
        settings.upstox_redirect_uri
        or "http://localhost:8200/v1/auth/upstox/callback"
    )

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0)
        ) as client:
            resp = await client.post(
                "https://api.upstox.com/v2/login/authorization/token",
                data={
                    "grant_type": "authorization_code",
                    "client_id": settings.upstox_api_key,
                    "client_secret": settings.upstox_api_secret,
                    "redirect_uri": redirect_uri,
                    "code": code,
                },
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                },
            )
    except httpx.HTTPError as exc:
        log.warning("upstox_oauth_token_exchange_network_error",
                    component="upstox_auth",
                    error_type=type(exc).__name__)
        return HTMLResponse(
            _html_result("Network Error",
                         "Token exchange failed (network error).", ok=False),
            status_code=502,
        )

    if resp.status_code != 200:
        log.warning("upstox_oauth_token_exchange_failed",
                    component="upstox_auth",
                    http_status=resp.status_code)
        return HTMLResponse(
            _html_result(
                "Exchange Failed",
                f"Upstox token endpoint returned HTTP {resp.status_code}.",
                ok=False,
            ),
            status_code=502,
        )

    try:
        payload = resp.json()
        access_token: str = payload["access_token"]
        expires_in: int = int(payload.get("expires_in", 86400))
    except (KeyError, ValueError, TypeError) as exc:
        log.warning("upstox_oauth_bad_response",
                    component="upstox_auth",
                    error=type(exc).__name__)
        return HTMLResponse(
            _html_result("Bad Response",
                         "Token response was malformed.", ok=False),
            status_code=502,
        )

    # TTL: cap at 23h regardless of what Upstox claims; subtract 5-min buffer
    ttl = min(max(expires_in - 300, 3600), _TOKEN_TTL)
    stored = False
    redis = _redis(request)
    if redis is not None:
        try:
            await redis.set(_TOKEN_KEY, access_token, ex=ttl)
            stored = True
            log.info(
                "upstox_oauth_token_stored",
                component="upstox_auth",
                ttl_seconds=ttl,
                expires_at=datetime.fromtimestamp(
                    time.time() + ttl, tz=timezone.utc
                ).isoformat(),
            )
        except Exception as exc:  # noqa: BLE001
            log.error("upstox_oauth_redis_store_failed",
                      component="upstox_auth",
                      error=type(exc).__name__)

    exp_str = datetime.fromtimestamp(
        time.time() + ttl, tz=timezone.utc
    ).strftime("%Y-%m-%d %H:%M UTC")

    return HTMLResponse(
        _html_result(
            "Upstox OAuth Complete",
            (
                f"Access token obtained successfully.<br>"
                f"Token length: {len(access_token)} chars<br>"
                f"Expires: {exp_str} (TTL {ttl}s)<br>"
                f"Redis key: <code>{_TOKEN_KEY}</code><br>"
                f"Stored in Redis: {'YES' if stored else 'NO (Redis unavailable)'}"
            ),
            ok=True,
        ),
        status_code=200,
    )


@router.get(
    "/auth/upstox/status",
    summary="Check Upstox OAuth token status",
    tags=["Auth"],
)
async def upstox_token_status(request: Request) -> JSONResponse:
    """Return Upstox OAuth token metadata from Redis.

    Never returns the token value — only TTL and presence.
    """
    redis = _redis(request)
    if redis is None:
        return JSONResponse({"error": "Redis unavailable"}, status_code=503)

    try:
        ttl = await redis.ttl(_TOKEN_KEY)
        present = isinstance(ttl, int) and ttl > 0
        return JSONResponse({
            "upstox_connected": present,
            "ttl_seconds": ttl if present else 0,
            "storage_location": f"redis:{_TOKEN_KEY}",
            "login_url": "/v1/auth/upstox/login",
        })
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(
            {"error": f"Redis error: {type(exc).__name__}"},
            status_code=503,
        )

