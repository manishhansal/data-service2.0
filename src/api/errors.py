"""
src/api/errors.py

Canonical error response envelope for DATA-SERVICE 2.0.

All API endpoints must use ``canonical_error`` / ``json_error_response``
instead of building error dicts by hand.  This module is the **single source
of truth** for the error shape defined in Requirement 16.3:

    {
        "error": {
            "code":         <ErrorCode string>,
            "message":      <human-readable string>,
            "provider":     <ProviderId | null>,
            "retryAfterMs": <int | null>,
            "requestId":    <non-empty string>
        }
    }

Rules enforced here:
- No stack traces, no credentials, no internal file paths in any error response
  (Requirements 16.3, 19.4, 19.5).
- ``requestId`` is always a non-empty string.  A UUID v4 is generated when the
  caller does not supply one.

Usage::

    from src.api.errors import ErrorCode, json_error_response

    return json_error_response(
        ErrorCode.INTERVAL_NOT_SUPPORTED,
        "interval 3m is permanently unsupported",
        status_code=400,
    )

Requirements: 16.3, 16.4, 19.4, 19.5
"""

from __future__ import annotations

import json
import uuid
from enum import Enum
from typing import Optional

from fastapi import Response


# ---------------------------------------------------------------------------
# Error codes
# ---------------------------------------------------------------------------


class ErrorCode(str, Enum):
    """Canonical error codes used across the Platform API.

    Inherits from ``str`` so that the enum value can be used directly as a
    JSON-serialisable string without calling ``.value``.
    """

    # ── Authentication / authorisation ──────────────────────────────────
    UNAUTHORIZED = "UNAUTHORIZED"
    INVALID_API_KEY = "INVALID_API_KEY"
    TOKEN_EXPIRED = "TOKEN_EXPIRED"
    INVALID_TOKEN = "INVALID_TOKEN"

    # ── Rate limiting ────────────────────────────────────────────────────
    RATE_LIMIT_EXCEEDED = "RATE_LIMIT_EXCEEDED"

    # ── Resource lookup ──────────────────────────────────────────────────
    NOT_FOUND = "NOT_FOUND"
    OBSERVATION_NOT_FOUND = "OBSERVATION_NOT_FOUND"
    TRADE_NOT_FOUND = "TRADE_NOT_FOUND"

    # ── Parameter validation ─────────────────────────────────────────────
    CURRENCY_NOT_SUPPORTED = "CURRENCY_NOT_SUPPORTED"
    INTERVAL_NOT_SUPPORTED = "INTERVAL_NOT_SUPPORTED"
    RESOLUTION_NOT_SUPPORTED = "RESOLUTION_NOT_SUPPORTED"
    INVALID_PARAMETER = "INVALID_PARAMETER"
    MISSING_FIELD = "MISSING_FIELD"
    INVALID_FIELD = "INVALID_FIELD"

    # ── Provider / upstream errors ───────────────────────────────────────
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    PROVIDER_QUEUE_FULL = "PROVIDER_QUEUE_FULL"

    # ── Streaming / event bus ────────────────────────────────────────────
    EVENT_PUBLISH_FAILED = "EVENT_PUBLISH_FAILED"
    STREAMING_ENGINE_UNAVAILABLE = "STREAMING_ENGINE_UNAVAILABLE"

    # ── Quality / computation ────────────────────────────────────────────
    EVALUATION_ERROR = "EVALUATION_ERROR"
    COMPUTATION_ERROR = "COMPUTATION_ERROR"

    # ── Provenance / lineage ─────────────────────────────────────────────
    PROVENANCE_IMMUTABLE = "PROVENANCE_IMMUTABLE"

    # ── Service availability ─────────────────────────────────────────────
    FNO_UNIVERSE_UNAVAILABLE = "FNO_UNIVERSE_UNAVAILABLE"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"


# ---------------------------------------------------------------------------
# HTTP status code mapping (Requirement 16.4)
# ---------------------------------------------------------------------------

#: Suggested HTTP status code for each ErrorCode.  Callers may override via
#: the ``status_code`` parameter of :func:`json_error_response`.
_DEFAULT_HTTP_STATUS: dict[ErrorCode, int] = {
    ErrorCode.UNAUTHORIZED: 401,
    ErrorCode.INVALID_API_KEY: 401,
    ErrorCode.TOKEN_EXPIRED: 401,
    ErrorCode.INVALID_TOKEN: 401,
    ErrorCode.RATE_LIMIT_EXCEEDED: 429,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.OBSERVATION_NOT_FOUND: 404,
    ErrorCode.TRADE_NOT_FOUND: 404,
    ErrorCode.CURRENCY_NOT_SUPPORTED: 400,
    ErrorCode.INTERVAL_NOT_SUPPORTED: 400,
    ErrorCode.RESOLUTION_NOT_SUPPORTED: 400,
    ErrorCode.INVALID_PARAMETER: 400,
    ErrorCode.MISSING_FIELD: 400,
    ErrorCode.INVALID_FIELD: 400,
    ErrorCode.PROVIDER_UNAVAILABLE: 502,
    ErrorCode.PROVIDER_QUEUE_FULL: 503,
    ErrorCode.EVENT_PUBLISH_FAILED: 502,
    ErrorCode.STREAMING_ENGINE_UNAVAILABLE: 503,
    ErrorCode.EVALUATION_ERROR: 422,
    ErrorCode.COMPUTATION_ERROR: 422,
    ErrorCode.PROVENANCE_IMMUTABLE: 405,
    ErrorCode.FNO_UNIVERSE_UNAVAILABLE: 503,
    ErrorCode.SERVICE_UNAVAILABLE: 503,
}


def default_status_for(code: "ErrorCode | str") -> int:
    """Return the default HTTP status code for *code*.

    Falls back to ``400`` for unknown codes so that callers always get a
    valid HTTP error response.
    """
    if isinstance(code, ErrorCode):
        return _DEFAULT_HTTP_STATUS.get(code, 400)
    # Try to match a string against the enum values.
    try:
        return _DEFAULT_HTTP_STATUS.get(ErrorCode(code), 400)
    except ValueError:
        return 400


# ---------------------------------------------------------------------------
# Core builder
# ---------------------------------------------------------------------------


def canonical_error(
    code: "ErrorCode | str",
    message: str,
    *,
    request_id: Optional[str] = None,
    provider: Optional[str] = None,
    retry_after_ms: Optional[int] = None,
) -> dict:
    """Build the canonical error envelope dict.

    Args:
        code:            An :class:`ErrorCode` member or a raw string code.
                         Using an ``ErrorCode`` enum value is preferred.
        message:         Human-readable description of the error.  Must not
                         contain stack traces, credential values, or internal
                         file paths.
        request_id:      Caller-supplied request identifier (e.g. from the
                         ``X-Request-Id`` header).  A UUID v4 is generated
                         when *None*.
        provider:        Provider identifier when the error is provider-
                         specific (e.g. ``"angel_one"``), or *None*.
        retry_after_ms:  Milliseconds the client should wait before retrying,
                         or *None*.

    Returns:
        A dict matching the canonical error shape::

            {
                "error": {
                    "code":         str,
                    "message":      str,
                    "provider":     str | None,
                    "retryAfterMs": int | None,
                    "requestId":    str,
                }
            }
    """
    # Normalise code to its string value — works for both str and Enum.
    code_str: str = code.value if isinstance(code, ErrorCode) else str(code)

    # requestId must always be a non-empty string.
    rid: str = request_id if (request_id and request_id.strip()) else str(uuid.uuid4())

    return {
        "error": {
            "code": code_str,
            "message": message,
            "provider": provider,
            "retryAfterMs": retry_after_ms,
            "requestId": rid,
        }
    }


# ---------------------------------------------------------------------------
# FastAPI response convenience wrapper
# ---------------------------------------------------------------------------


def json_error_response(
    code: "ErrorCode | str",
    message: str,
    *,
    status_code: Optional[int] = None,
    request_id: Optional[str] = None,
    provider: Optional[str] = None,
    retry_after_ms: Optional[int] = None,
) -> Response:
    """Return a :class:`fastapi.Response` containing the canonical error JSON.

    The response:
    - Has ``Content-Type: application/json``.
    - Sets ``Cache-Control: no-store`` (Requirement 16.9 — errors must not be
      cached).
    - Carries no stack traces, no credentials, and no internal file paths.

    Args:
        code:           An :class:`ErrorCode` or a raw string error code.
        message:        Human-readable error description.
        status_code:    HTTP status code.  When *None*, the default for *code*
                        is used (see :func:`default_status_for`).
        request_id:     Optional caller-supplied request identifier.
        provider:       Optional provider identifier.
        retry_after_ms: Optional retry hint in milliseconds.

    Returns:
        A :class:`fastapi.Response` ready to be returned from any endpoint.
    """
    http_status = status_code if status_code is not None else default_status_for(code)
    body = json.dumps(
        canonical_error(
            code,
            message,
            request_id=request_id,
            provider=provider,
            retry_after_ms=retry_after_ms,
        )
    )
    return Response(
        content=body,
        status_code=http_status,
        media_type="application/json",
        headers={"Cache-Control": "no-store"},
    )
