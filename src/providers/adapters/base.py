"""
Base error classes for provider adapters.

All provider adapters raise subclasses of ``ProviderError`` so that the
Provider_Gateway can handle failures uniformly regardless of which adapter
raised them.  The circuit breaker and rate limiter consume these exceptions
to decide whether to increment failure counters.

Design contract
---------------
* ``ProviderRateLimitedError`` maps to HTTP 429 from upstream — the circuit
  breaker MUST NOT increment its failure counter for this error.
* ``ProviderMarketClosedError`` represents empty-data responses during
  closed market hours — the circuit breaker MUST NOT count these as failures
  (Requirement 5.7).
* ``ProviderUnsupportedError`` maps to a provider returning data it
  advertised but cannot actually deliver — the circuit breaker MUST NOT
  count this as a failure (Requirement 5.8).

Requirements: 5.4, 5.7, 5.8
"""

from __future__ import annotations

from typing import Optional


class ProviderError(Exception):
    """Base class for all provider adapter errors.

    All concrete error subclasses inherit from this class so callers can
    catch the full hierarchy with a single ``except ProviderError`` clause.

    Attributes:
        message:      Human-readable error description.
        provider:     The provider identifier string (e.g. ``"scrapling_nse"``).
        status_code:  HTTP status code returned by the upstream source, or
                      ``None`` when the error is a transport-level failure.
        retry_after_s: Seconds the caller should wait before retrying.
                       Populated from the upstream ``Retry-After`` header when
                       available; ``None`` otherwise.
    """

    def __init__(
        self,
        message: str,
        *,
        provider: str = "",
        status_code: Optional[int] = None,
        retry_after_s: Optional[int] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.provider = provider
        self.status_code = status_code
        self.retry_after_s = retry_after_s

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"{self.__class__.__name__}("
            f"provider={self.provider!r}, "
            f"status_code={self.status_code!r}, "
            f"message={self.message!r})"
        )


class ProviderUnavailableError(ProviderError):
    """Raised when the provider is temporarily unreachable or returns HTTP 5xx.

    Triggers circuit-breaker failure counting.

    Typical causes:
    - HTTP 503 from the upstream host
    - Connection timeout / connection refused
    - DNS resolution failure
    """


class ProviderRateLimitedError(ProviderError):
    """Raised when the provider returns HTTP 429 (rate limit exceeded).

    The circuit breaker MUST NOT increment its failure counter for this error
    (Requirement 5.5 / 5.6).

    ``retry_after_s`` is populated from the ``Retry-After`` response header
    when present.  If the header is absent, callers apply a default 60-second
    backoff (Requirement 5.6).
    """


class ProviderAuthError(ProviderError):
    """Raised when the provider returns HTTP 401 or 403.

    For authenticated providers (Angel One, Upstox), this signals that a
    token rotation or OAuth refresh is needed.  For credential-free providers
    (Scrapling/NSE, Jugaad-data, OpenChart) an HTTP 403 usually means WAF
    blocking and should be treated like a temporary unavailability.
    """


class ProviderDataError(ProviderError):
    """Raised when the provider returns a malformed or unparseable response.

    This covers:
    - Non-JSON body when JSON is expected
    - Missing required fields in an otherwise valid HTTP 200 response
    - Unexpected response structure that the adapter cannot normalise

    Triggers circuit-breaker failure counting (the provider is misbehaving).
    """


class ProviderMarketClosedError(ProviderError):
    """Raised when the provider returns an empty dataset because the market is closed.

    The circuit breaker MUST NOT count this as a provider failure
    (Requirement 5.7).  The Market_Engine handles this gracefully by returning
    the last available quote with ``marketStatus: "CLOSED"``.
    """


class ProviderUnsupportedError(ProviderError):
    """Raised when the provider cannot serve the requested capability.

    The circuit breaker MUST NOT count this as a provider failure
    (Requirement 5.8).  The Gateway will re-route the request using the
    Capability_Matrix rather than marking the provider as unhealthy.

    Examples:
    - Requesting ``3m`` interval from an Indian-market provider
    - Requesting option chain data from a provider that only serves EOD
    """
