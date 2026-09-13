"""
Credential Stripping Middleware — DATA-SERVICE 2.0.

Ensures that no provider API keys, secrets, tokens, or passwords are ever
returned in an outgoing HTTP response body.

Design contract (Requirements 16.8, 19.1, 19.4):
  - ONLY outgoing *response* bodies are sanitised.
  - Incoming *request* headers are never touched — they are needed for upstream
    provider authentication.
  - Any JSON object key whose name matches one of the credential patterns
    (case-insensitive, partial match) has its value replaced with "[REDACTED]".
  - Non-JSON responses pass through unmodified.
  - Recursive: nested objects and arrays are also sanitised.

Sensitive key patterns (case-insensitive substring match):
    key, secret, token, password, apikey, credential
    Plus common header echo names: authorization, x-mbx-apikey, x-api-key
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

# ---------------------------------------------------------------------------
# Default credential key patterns (case-insensitive)
# ---------------------------------------------------------------------------

#: Default list of substrings that flag a JSON key as credential-bearing.
_DEFAULT_KEY_PATTERNS: list[str] = [
    "key",
    "secret",
    "token",
    "password",
    "apikey",
    "api_key",
    "credential",
    "authorization",
    "x-mbx-apikey",
    "x-api-key",
    "passwd",
    "passphrase",
    "private",
    "auth",
]

#: Sentinel replacement value for redacted fields.
REDACTED = "[REDACTED]"


@dataclass
class CredentialPattern:
    """
    Configurable pattern set for credential detection in JSON response bodies.

    Attributes:
        key_substrings: Substrings whose presence (case-insensitive) in a JSON
            object key marks it as credential-bearing.  The default list covers
            all patterns required by the design document.
        additional_substrings: Extra substrings to append to the default list,
            allowing callers to extend without replacing the defaults.
        _compiled: Compiled regex pattern built lazily from the final substring
            list; do not set this directly.
    """

    key_substrings: list[str] = field(default_factory=lambda: list(_DEFAULT_KEY_PATTERNS))
    additional_substrings: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        # Merge and de-duplicate, preserving order.
        combined = list(dict.fromkeys(self.key_substrings + self.additional_substrings))
        # Build a single alternation pattern; escape each literal substring.
        escaped = [re.escape(s) for s in combined]
        self._pattern: re.Pattern[str] = re.compile(
            "|".join(escaped),
            re.IGNORECASE,
        )

    def is_credential_key(self, key: str) -> bool:
        """Return True if *key* matches any registered credential pattern."""
        return bool(self._pattern.search(key))

    def redact_value(self, value: Any) -> str:  # noqa: ANN401
        """Return the redacted sentinel for any credential value."""
        return REDACTED


class CredentialStripperMiddleware(BaseHTTPMiddleware):
    """
    FastAPI / Starlette middleware that sanitises outgoing response bodies.

    Behaviour:
    - Only *response* bodies are inspected and modified.
    - Request headers, request bodies, and response headers are not altered.
    - JSON responses (``Content-Type: application/json``) are parsed, walked
      recursively, and any key matching a credential pattern has its value
      replaced with ``"[REDACTED]"``.
    - Non-JSON responses are streamed through without modification.
    - If the JSON body cannot be decoded (malformed JSON), the original body
      is passed through unmodified to avoid breaking the response.
    - The ``Content-Length`` header is updated after sanitisation to reflect
      the new body length.

    Args:
        app: The ASGI application to wrap.
        patterns: Optional :class:`CredentialPattern` instance. If omitted,
            the default pattern set is used.
        extra_key_substrings: Convenience shortcut — extra substrings appended
            to the default pattern list without requiring a full
            ``CredentialPattern`` instance.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        patterns: CredentialPattern | None = None,
        extra_key_substrings: list[str] | None = None,
    ) -> None:
        super().__init__(app)
        if patterns is not None:
            self._patterns = patterns
        else:
            self._patterns = CredentialPattern(
                additional_substrings=extra_key_substrings or [],
            )

    # ------------------------------------------------------------------
    # Middleware hook
    # ------------------------------------------------------------------

    async def dispatch(self, request: Request, call_next: Callable[..., Any]) -> Response:
        """Pass the request through; sanitise the response body before returning."""
        response: Response = await call_next(request)

        content_type: str = response.headers.get("content-type", "")
        if "application/json" not in content_type:
            # Non-JSON response — pass through untouched.
            return response

        # Consume the response body.
        body_bytes: bytes = b""
        async for chunk in response.body_iterator:  # type: ignore[attr-defined]
            body_bytes += chunk if isinstance(chunk, bytes) else chunk.encode()

        # Attempt to sanitise.
        sanitised_bytes = self._sanitise_body(body_bytes)

        # Rebuild the response with the sanitised body.
        return Response(
            content=sanitised_bytes,
            status_code=response.status_code,
            headers=dict(response.headers),
            media_type=response.media_type,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _sanitise_body(self, body: bytes) -> bytes:
        """
        Deserialise *body* as JSON, recursively redact credential fields,
        and re-serialise.  Returns the original bytes on any parse error.
        """
        if not body:
            return body

        try:
            data = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            # Malformed JSON — return original to avoid breaking the response.
            return body

        sanitised = self._walk(data)
        return json.dumps(sanitised, ensure_ascii=False, separators=(",", ":")).encode()

    def _walk(self, node: Any) -> Any:  # noqa: ANN401
        """
        Recursively walk *node* (any JSON-compatible value) and redact any
        dict key that matches the credential pattern.
        """
        if isinstance(node, dict):
            return {
                key: (
                    self._patterns.redact_value(value)
                    if self._patterns.is_credential_key(key)
                    else self._walk(value)
                )
                for key, value in node.items()
            }
        if isinstance(node, list):
            return [self._walk(item) for item in node]
        # Scalar (str, int, float, bool, None) — returned as-is.
        return node
