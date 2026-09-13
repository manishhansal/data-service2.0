"""
src/api/contract.py

DataParityContract REST endpoint for DATA-SERVICE 2.0.

``GET /v1/contract``
    Returns the current :class:`DataParityContract` instance — a
    machine-readable document that declares every correctness invariant
    consumers may rely on when integrating with the Platform.

Key invariants exposed by this endpoint:
    * ``3m`` interval is permanently banned for Indian market data.
    * ``oi`` is never populated from ``tradedValue``.
    * ``iv`` is ``null`` when absent; zero is never a substitute.
    * All optional numeric fields are ``null`` when not supplied — placeholder
      zeros are prohibited.
    * All four data modes (live, paper, replay, backtest) route through the
      same normalisation and validation pipeline.

The contract object is immutable: any attempt to mutate it (POST, PUT,
PATCH, DELETE) returns HTTP 405.

All responses use the canonical success envelope:
    {"data": <DataParityContract>, "metadata": {...}}

Requirements: 1.5, 6.2, 6.4, 23.1, 23.2
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

import structlog
from fastapi import APIRouter
from fastapi.responses import Response

from src.core.schemas.parity import DataParityContract, build_contract

router = APIRouter()
_log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Module-level contract singleton
#
# The contract is generated once at import time and served from memory on
# every request.  ``generatedAt`` reflects the moment the module was first
# loaded, which is the process-start time in practice.
#
# ``parityVerified`` defaults to False; it can be set to True via
# ``set_parity_verified()`` once the four-mode pipeline check passes
# (see Task 14.2 design — Requirement 23.2).
# ---------------------------------------------------------------------------

_contract: DataParityContract = build_contract(
    parity_verified=False,
    last_verified_at=None,
)


def set_parity_verified(*, last_verified_at: str) -> None:
    """Update the module-level contract singleton to mark parity as verified.

    Called by the parity verification logic (Task 14.2) once all four data
    modes (live, paper, replay, backtest) have been confirmed to share the
    same normalisation and validation pipeline.

    This replaces the singleton with a new frozen :class:`DataParityContract`
    instance that has ``parityVerified=True`` and ``lastVerifiedAt`` set to
    *last_verified_at*.

    Parameters
    ----------
    last_verified_at:
        UTC ISO-8601 string of the verification timestamp.
    """
    global _contract  # noqa: PLW0603
    now = datetime.now(timezone.utc)
    generated_at = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"

    _contract = DataParityContract(
        parityVerified=True,
        lastVerifiedAt=last_verified_at,
        generatedAt=generated_at,
    )
    _log.info(
        "parity_contract_verified",
        last_verified_at=last_verified_at,
    )


def get_current_contract() -> DataParityContract:
    """Return the current module-level :class:`DataParityContract` singleton.

    Exposed as a public helper so tests and other modules can inspect the
    contract without going through the HTTP layer.
    """
    return _contract


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------


def _utc_iso_now() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _request_id() -> str:
    return str(uuid.uuid4())


def _success_envelope(data: Any) -> dict[str, Any]:
    return {
        "data": data,
        "metadata": {
            "requestedAt": _utc_iso_now(),
            "dataSourceType": "DERIVED",
            "provider": None,
            "provenance": None,
        },
    }


def _error_envelope(
    code: str,
    message: str,
    *,
    request_id: str | None = None,
) -> dict[str, Any]:
    return {
        "error": {
            "code": code,
            "message": message,
            "provider": None,
            "retryAfterMs": None,
            "requestId": request_id or _request_id(),
        }
    }


def _json_response(content: Any, *, status_code: int = 200) -> Response:
    return Response(
        content=json.dumps(content, ensure_ascii=False),
        status_code=status_code,
        media_type="application/json",
    )


# ---------------------------------------------------------------------------
# GET /v1/contract
# ---------------------------------------------------------------------------


@router.get(
    "/contract",
    summary="DataParityContract",
    description=(
        "Returns the current DataParityContract — the machine-readable document "
        "that declares every correctness invariant DATA-SERVICE 2.0 guarantees to "
        "its consumers.  "
        "Key invariants: 3m interval permanently banned for India; oi never from "
        "tradedValue; iv null when absent (zero is not a substitute); all four data "
        "modes (live, paper, replay, backtest) share the same normalisation pipeline.  "
        "(Requirements 1.5, 6.2, 6.4, 23.1, 23.2)"
    ),
    response_class=Response,
    tags=["Contract"],
)
async def get_contract() -> Response:
    """Return the current DataParityContract.

    The contract is a frozen, machine-readable document listing all
    correctness invariants, supported intervals, prohibited intervals, and
    semantic integrity rules that the Platform guarantees.

    Response shape::

        {
            "data": {
                "contractVersion": "2.0.0",
                "marketDataApiVersion": "v1",
                "supportedMarkets": ["india", "crypto"],
                "supportedIntervals": {
                    "india": ["1m", "5m", "10m", "15m", "30m", "1h", "1d", "1w", "1M"],
                    "crypto": ["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d"]
                },
                "guarantees": ["..."],
                "prohibitedIntervals": {"india": ["3m"], "crypto": []},
                "nullSemantics": "...",
                "oiSourceRule": "...",
                "ivRule": "...",
                "parityVerified": false,
                "lastVerifiedAt": null,
                "liveDataPath": "src.engines.market_engine",
                "paperDataPath": "src.engines.market_engine",
                "replayDataPath": "src.engines.replay_engine",
                "backtestDataPath": "src.engines.backtest_engine",
                "generatedAt": "2026-01-15T09:15:00.000Z"
            },
            "metadata": {
                "requestedAt": "2026-01-15T09:15:01.234Z",
                "dataSourceType": "DERIVED",
                "provider": null,
                "provenance": null
            }
        }
    """
    contract = get_current_contract()

    # Serialise via Pydantic to get the full dict (respects field aliases,
    # excludes, etc.) then encode to JSON.
    contract_dict = contract.model_dump(mode="json")

    # Rename parityVerified key to match the spec field name casing
    # (model field is parityVerified but we stored it as parity_verified
    # in the build_contract call — Pydantic handles the mapping)
    payload = _success_envelope(contract_dict)

    _log.debug("contract_served", contract_version=contract.contractVersion)

    return _json_response(payload)


# ---------------------------------------------------------------------------
# 405 Method Not Allowed for mutation attempts
# ---------------------------------------------------------------------------


@router.post(
    "/contract",
    include_in_schema=False,
    response_class=Response,
)
@router.put(
    "/contract",
    include_in_schema=False,
    response_class=Response,
)
@router.patch(
    "/contract",
    include_in_schema=False,
    response_class=Response,
)
@router.delete(
    "/contract",
    include_in_schema=False,
    response_class=Response,
)
async def contract_mutation_rejected() -> Response:
    """Reject all mutation attempts with HTTP 405.

    The DataParityContract is read-only; any attempt to modify it is
    rejected with HTTP 405 and the contract remains unchanged.
    (Requirement 8.9 pattern applied to the contract endpoint.)
    """
    return _json_response(
        _error_envelope(
            "METHOD_NOT_ALLOWED",
            "The DataParityContract is read-only. Mutation is not permitted.",
        ),
        status_code=405,
    )
