"""
Provider Gateway — skeleton entry point for Phase 4.

The ``ProviderGateway`` is the **single egress point** for all outbound
provider calls.  No other component may call a provider directly.

This module implements task 4.4 — the provider-switch logger — and provides
stub entry points that will be fleshed out in tasks 4.5–4.9:

- ``log_provider_switch()``  (4.4 — this task)
- ``get_provider_health()``  stub (4.9 will replace it)
- ``fetch()``                stub (4.5–4.8 will wire adapters)

Design contract
---------------
* ``log_provider_switch`` always emits a structured log entry at INFO level
  with every field required by the design spec (§ Provider Switch Logging).
* Timestamp is always UTC ISO-8601 with ``Z`` suffix.
* ``SwitchReason`` is the exhaustive enumeration of valid reasons; callers
  MUST use enum values — plain strings are rejected by the type system.

Requirements: 5.10
"""

from __future__ import annotations

import datetime
from enum import Enum
from typing import Any

import structlog

from src.core.schemas.provider import DataType, ProviderId
from src.observability.logging import get_logger

logger: structlog.stdlib.BoundLogger = get_logger(__name__)


# ---------------------------------------------------------------------------
# SwitchReason enumeration
# ---------------------------------------------------------------------------


class SwitchReason(str, Enum):
    """Exhaustive set of reasons that can trigger a provider switch.

    Using ``str`` as the mixin makes enum values JSON-serialisable by default
    and compatible with structlog's log rendering.
    """

    CIRCUIT_BREAKER_OPEN = "circuit_breaker_open"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    AUTH_FAILED = "auth_failed"
    UNSUPPORTED_CAPABILITY = "unsupported_capability"
    QUALITY_BELOW_THRESHOLD = "quality_below_threshold"
    MARKET_CLOSED = "market_closed"


# ---------------------------------------------------------------------------
# ProviderGateway
# ---------------------------------------------------------------------------


class ProviderGateway:
    """Single egress point for all outbound provider calls.

    Phase-4 skeleton: only the provider-switch logger, a health-check stub,
    and a fetch stub are implemented here.  Subsequent tasks (4.5–4.9) will
    wire the actual provider adapters, circuit breakers, and rate limiters.
    """

    # ------------------------------------------------------------------ #
    # 4.4 — Provider switch logger (Requirement 5.10)
    # ------------------------------------------------------------------ #

    @staticmethod
    def log_provider_switch(
        *,
        from_provider: ProviderId,
        to_provider: ProviderId,
        reason: SwitchReason,
        dataset: DataType,
        instrument_id: str,
    ) -> None:
        """Emit a structured log entry when the gateway switches providers.

        When a primary provider fails and a fallback is selected, this method
        MUST be called so that the switch is observable in logs and traces.

        The log entry shape matches the design spec exactly:

        .. code-block:: json

            {
                "event": "provider_switch",
                "fromProvider": "angel_one",
                "toProvider": "openchart",
                "reason": "circuit_breaker_open",
                "dataset": "HISTORICAL_OHLCV",
                "instrumentId": "NSE:RELIANCE:EQ",
                "timestamp": "2026-01-15T09:15:00.000Z"
            }

        Args:
            from_provider:  The provider that failed or was bypassed.
            to_provider:    The fallback provider that will serve the request.
            reason:         Why the switch occurred (``SwitchReason`` enum).
            dataset:        The data type being served (``DataType`` enum).
            instrument_id:  Canonical instrument ID, e.g. ``"NSE:RELIANCE:EQ"``.
        """
        timestamp: str = (
            datetime.datetime.now(datetime.timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%S.") +
            # milliseconds + Z suffix for strict UTC ISO-8601 with ms precision
            f"{datetime.datetime.now(datetime.timezone.utc).microsecond // 1000:03d}Z"
        )

        logger.info(
            "provider_switch",
            component="provider_gateway",
            fromProvider=from_provider.value,
            toProvider=to_provider.value,
            reason=reason.value,
            dataset=dataset.value,
            instrumentId=instrument_id,
            timestamp=timestamp,
        )

    # ------------------------------------------------------------------ #
    # 4.9 — provider health (Requirement 5.11)
    # ------------------------------------------------------------------ #

    def get_provider_health(self) -> dict[str, dict[str, Any]]:
        """Return a baseline per-provider × per-capability health structure.

        This method is called by ``GET /v1/providers/health`` (Task 4.9) as
        the initial data template.  The API handler enriches each entry with
        live circuit-breaker state read from Redis before returning it to the
        consumer.

        The returned dict is keyed by ``"{provider_id}:{data_type}"`` to give
        one entry per provider × capability combination that appears in the
        Capability_Matrix.  All metric fields start at their safe defaults
        (UNKNOWN status, CLOSED circuit, 1.0 availability, 0.0 error rate,
        ``True`` semantic integrity) so the response is always well-formed
        even before live metrics are available.

        Returns:
            A dict keyed by ``"{provider_id}:{data_type}"`` where each value
            is a health record with fields:

            * ``status``            — UP | DOWN | DEGRADED | UNKNOWN
            * ``circuitState``      — CLOSED | OPEN | HALF_OPEN
            * ``availability``      — float 0.0–1.0
            * ``latencyP50Ms``      — int ms (None until measured)
            * ``latencyP99Ms``      — int ms (None until measured)
            * ``errorRate``         — float 0.0–1.0
            * ``lastSuccessAt``     — UTC ISO-8601 string or None
            * ``lastFailureReason`` — string or None
            * ``semanticIntegrity`` — bool (True = last response was semantically valid)
            * ``provider``          — provider ID value (str)
            * ``capability``        — DataType value (str)
        """
        from src.providers.capability_matrix import _MATRIX  # noqa: PLC0415

        result: dict[str, dict[str, Any]] = {}

        for cap in _MATRIX:
            key = f"{cap.provider.value}:{cap.dataType.value}"
            if key in result:
                # Only keep one entry per provider × capability pair;
                # multiple instrument-class rows for the same pair are
                # collapsed (the health status is per provider × data-type,
                # not per instrument class).
                continue
            result[key] = {
                "provider": cap.provider.value,
                "capability": cap.dataType.value,
                "status": "UNKNOWN",
                "circuitState": "CLOSED",
                "availability": 1.0,
                "latencyP50Ms": None,
                "latencyP99Ms": None,
                "errorRate": 0.0,
                "lastSuccessAt": None,
                "lastFailureReason": None,
                "semanticIntegrity": True,
            }

        return result

    # ------------------------------------------------------------------ #
    # 4.5–4.8 stub — adapter dispatch (will be replaced when adapters land)
    # ------------------------------------------------------------------ #

    async def fetch(
        self,
        provider_id: ProviderId,
        data_type: DataType,
        instrument_class: str,
        **kwargs: Any,
    ) -> Any:
        """Dispatch a data-fetch request to the appropriate provider adapter.

        This is a stub.  Tasks 4.5–4.8 will wire the concrete adapters
        (Scrapling/NSE, Angel One, Upstox, Jugaad, OpenChart, Yahoo Finance,
        Binance, Deribit) behind this dispatch point, guarded by the
        circuit breaker and rate limiter added in tasks 4.2–4.3.

        Args:
            provider_id:       Target provider.
            data_type:         The kind of data to fetch.
            instrument_class:  Instrument class (EQ, FO, IDX, CRYPTO_*, ALL).
            **kwargs:          Provider/data-type specific parameters
                               (symbol, interval, from_ts, to_ts, …).

        Returns:
            Provider response data (schema depends on data_type).

        Raises:
            NotImplementedError: Always, until the adapters are wired in.
        """
        raise NotImplementedError(
            f"fetch() not yet implemented for provider={provider_id.value}, "
            f"data_type={data_type.value}, instrument_class={instrument_class}. "
            "Adapters will be wired in tasks 4.5–4.8."
        )
