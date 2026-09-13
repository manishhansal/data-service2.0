"""
src/auth/consumer_auth.py

Consumer authentication for DATA-SERVICE 2.0.

Task 13.2 — Requirements 19.2

Supports two authentication methods accepted by all production API endpoints:

1. **API Key** — ``X-API-KEY: <key>`` header checked against the
   ``CONSUMER_API_KEYS`` comma-separated allowlist from settings.

2. **JWT Bearer** — ``Authorization: Bearer <token>`` with a HS256-signed
   JWT; required claims are ``sub`` (consumer ID) and ``exp`` (expiry);
   optional ``scopes`` claim.

``ConsumerAuthDependency`` is a FastAPI dependency that accepts either method.
Providing neither returns HTTP 401 with ``UNAUTHORIZED``.
A valid but expired token returns HTTP 401 with ``TOKEN_EXPIRED``.
A malformed or unrecognised credential returns HTTP 401 with ``INVALID_TOKEN``
or ``INVALID_API_KEY``.

**Token-exchange endpoint**

    GET /v1/auth/token?api_key=<key>

Exchange a valid API key for a short-lived JWT so downstream services that
can only carry a bearer token can authenticate after the initial key exchange.

**Security notes**

- The ``jwt_secret`` setting is never logged or included in any response.
- Credential-related field names are stripped by the credential-stripper
  middleware (Task 13.1) before serialisation; the token value returned by
  ``/v1/auth/token`` intentionally uses the name ``"accessToken"`` rather
  than ``"token"`` to avoid accidental stripping in that one response where
  the token *should* be returned to the caller.

All error responses use the canonical envelope:
    ``{"error": {"code": str, "message": str, "provider": null,
                 "retryAfterMs": null, "requestId": str}}``
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Optional

import jwt
import structlog
from fastapi import APIRouter, Header, HTTPException, Query, Request, Security
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer

from src.core.settings import get_settings

# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

_log = structlog.get_logger(__name__)

router = APIRouter()

# FastAPI security scheme extractors (used for OpenAPI schema generation).
_api_key_header_scheme = APIKeyHeader(name="X-API-KEY", auto_error=False)
_bearer_scheme = HTTPBearer(auto_error=False)

# JWT algorithm — HS256 is the only supported algorithm (no RS256 / ES256
# to keep the implementation dependency-free and symmetric-key simple).
_ALGORITHM = "HS256"


# ---------------------------------------------------------------------------
# Internal error helpers
# ---------------------------------------------------------------------------


def _request_id() -> str:
    return str(uuid.uuid4())


def _error_body(
    code: str,
    message: str,
    *,
    request_id: Optional[str] = None,
) -> dict[str, Any]:
    """Build the canonical error response envelope (Requirement 16.3)."""
    return {
        "error": {
            "code": code,
            "message": message,
            "provider": None,
            "retryAfterMs": None,
            "requestId": request_id or _request_id(),
        }
    }


def _http_401(code: str, message: str) -> HTTPException:
    """Return an HTTPException(401) with the canonical error envelope."""
    return HTTPException(
        status_code=401,
        detail=_error_body(code, message),
        headers={"WWW-Authenticate": "Bearer"},
    )


# ---------------------------------------------------------------------------
# ApiKeyAuth
# ---------------------------------------------------------------------------


class ApiKeyAuth:
    """Validate the ``X-API-KEY`` header against the configured allowlist.

    Usage as a FastAPI dependency::

        @router.get("/protected")
        async def endpoint(consumer: str = Depends(ApiKeyAuth())):
            ...

    Raises HTTP 401 when:
    - The header is absent
    - The key is not in the configured allowlist
    """

    def __call__(
        self,
        api_key: Annotated[Optional[str], Security(_api_key_header_scheme)] = None,
    ) -> str:
        """Return the consumer ID (the API key itself) or raise HTTP 401."""
        settings = get_settings()
        valid_keys = settings.consumer_api_keys_list

        if not api_key:
            raise _http_401("UNAUTHORIZED", "Missing X-API-KEY header.")

        if api_key not in valid_keys:
            _log.warning("api_key_rejected", key_prefix=api_key[:6] + "…" if len(api_key) > 6 else "…")
            raise _http_401("INVALID_API_KEY", "The provided API key is not valid.")

        return api_key


# ---------------------------------------------------------------------------
# JwtBearerAuth
# ---------------------------------------------------------------------------


class JwtBearerAuth:
    """Validate a HS256-signed JWT in the ``Authorization: Bearer`` header.

    Required claims: ``sub`` (consumer ID string), ``exp`` (Unix timestamp).
    Optional claim: ``scopes`` (list[str]).

    Usage as a FastAPI dependency::

        @router.get("/protected")
        async def endpoint(payload: dict = Depends(JwtBearerAuth())):
            consumer_id = payload["sub"]

    Raises HTTP 401 when:
    - The ``Authorization`` header is absent or malformed
    - The token signature is invalid
    - The token is expired (``code=TOKEN_EXPIRED``)
    - Required claims are missing
    """

    def __call__(
        self,
        credentials: Annotated[
            Optional[HTTPAuthorizationCredentials],
            Security(_bearer_scheme),
        ] = None,
    ) -> dict[str, Any]:
        """Decode and validate the JWT; return the verified payload dict."""
        if credentials is None or not credentials.credentials:
            raise _http_401("UNAUTHORIZED", "Missing Authorization: Bearer <token> header.")

        token = credentials.credentials
        settings = get_settings()

        try:
            payload: dict[str, Any] = jwt.decode(
                token,
                settings.jwt_secret,
                algorithms=[_ALGORITHM],
                options={"require": ["sub", "exp"]},
            )
        except jwt.ExpiredSignatureError:
            raise _http_401("TOKEN_EXPIRED", "The JWT has expired. Please obtain a new token.")
        except jwt.MissingRequiredClaimError as exc:
            raise _http_401(
                "INVALID_TOKEN",
                f"JWT is missing required claim: {exc}",
            )
        except jwt.InvalidTokenError as exc:
            # Generic catch for decode errors (bad signature, malformed, etc.).
            # Do not expose the raw exception message to avoid leaking internals.
            _log.warning("jwt_validation_failed", reason=type(exc).__name__)
            raise _http_401("INVALID_TOKEN", "The provided JWT is invalid.")

        return payload


# ---------------------------------------------------------------------------
# ConsumerAuthDependency
# ---------------------------------------------------------------------------


class ConsumerAuthDependency:
    """FastAPI dependency that accepts *either* an API key or a JWT bearer.

    Checks in order:
    1. ``X-API-KEY`` header — if present, validate against the allowlist.
    2. ``Authorization: Bearer`` header — if present, validate as JWT.
    3. If neither is present, return HTTP 401 with ``UNAUTHORIZED``.

    Usage::

        @router.get("/protected")
        async def endpoint(consumer=Depends(ConsumerAuthDependency())):
            # consumer is the API key string or the JWT payload dict
            ...

    Returns either:
    - ``str`` — the validated API key (when ``X-API-KEY`` was used)
    - ``dict[str, Any]`` — the decoded JWT payload (when bearer was used)
    """

    def __call__(
        self,
        request: Request,
        api_key: Annotated[Optional[str], Security(_api_key_header_scheme)] = None,
        credentials: Annotated[
            Optional[HTTPAuthorizationCredentials],
            Security(_bearer_scheme),
        ] = None,
    ) -> str | dict[str, Any]:
        """Authenticate the incoming request and return the consumer identity."""
        # Prefer the API key path — it is cheaper and has no crypto overhead.
        if api_key is not None:
            return ApiKeyAuth()(api_key=api_key)

        if credentials is not None:
            return JwtBearerAuth()(credentials=credentials)

        # Neither credential type was provided.
        raise _http_401(
            "UNAUTHORIZED",
            "Authentication required. Provide an X-API-KEY header or an "
            "Authorization: Bearer <token> header.",
        )


# ---------------------------------------------------------------------------
# Token-exchange endpoint
# ---------------------------------------------------------------------------


def _issue_jwt(consumer_id: str) -> str:
    """Mint a new HS256 JWT for *consumer_id* using the configured secret."""
    settings = get_settings()
    now = datetime.now(timezone.utc)
    payload = {
        "sub": consumer_id,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=settings.jwt_expiry_seconds)).timestamp()),
        "scopes": [],
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=_ALGORITHM)


@router.get(
    "/auth/token",
    summary="Exchange an API key for a short-lived JWT bearer token",
    description=(
        "Validates the supplied API key and issues a signed HS256 JWT that can be "
        "used in ``Authorization: Bearer <token>`` for subsequent requests.  "
        "The token lifetime is controlled by the ``JWT_EXPIRY_SECONDS`` setting "
        "(default 3600 s / 1 hour).  "
        "Requirement 19.2."
    ),
    tags=["Auth"],
)
async def get_token(
    api_key: Annotated[
        str,
        Query(
            alias="api_key",
            description="A valid API key from the configured allowlist.",
        ),
    ],
) -> dict[str, Any]:
    """Exchange a valid API key for a short-lived JWT.

    **Query parameter**: ``api_key`` — the consumer's API key.

    **Success response** (HTTP 200)::

        {
          "accessToken": "<signed JWT>",
          "tokenType": "Bearer",
          "expiresIn": 3600
        }

    Raises HTTP 401 when the API key is absent or invalid.
    """
    settings = get_settings()
    valid_keys = settings.consumer_api_keys_list

    if not api_key:
        raise _http_401("UNAUTHORIZED", "Missing api_key query parameter.")

    if api_key not in valid_keys:
        _log.warning("token_exchange_rejected", key_prefix=api_key[:6] + "…" if len(api_key) > 6 else "…")
        raise _http_401("INVALID_API_KEY", "The provided API key is not valid.")

    token = _issue_jwt(consumer_id=api_key)

    return {
        "accessToken": token,
        "tokenType": "Bearer",
        "expiresIn": settings.jwt_expiry_seconds,
    }
