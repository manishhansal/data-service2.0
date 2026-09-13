"""
src/api/quality.py

Quality Engine REST API endpoints for DATA-SERVICE 2.0.

Task 9.7 — Implement quality API endpoints.

Endpoints:
    POST /v1/quality/evaluate
        Accept a JSON body containing a market data observation dict.
        Run DataQualityGate.check() and QualityEngine.classify_quality().
        Return the gate result, classification, and metadata envelope.

    GET  /v1/quality/score
        Accept query params that map directly to
        QualityEngine.compute_confidence_from_inputs().
        Return the computed score, grade, signalEngineAllowed flag, and
        metadata envelope.

All responses use the canonical success envelope:
    {"data": <payload>, "metadata": {...}}

All error responses use the canonical error envelope:
    {"error": {"code": <str>, "message": <str>, "requestId": <str>}}

Requirements: 7.1, 7.2, 7.5, 7.6, 7.7, 7.9, 7.11
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Annotated, Any, Optional

import structlog
from fastapi import APIRouter, Body, Query, Request
from fastapi.responses import Response

from src.engines.quality_engine import DataQualityGate, QualityEngine

router = APIRouter()
_log = structlog.get_logger(__name__)
_engine = QualityEngine()

# Valid freshness classification strings accepted by the score endpoint.
_VALID_FRESHNESS = frozenset({"FRESH", "AGING", "STALE", "EXPIRED", "UNKNOWN"})


# ---------------------------------------------------------------------------
# Response helpers (mirror the pattern used in india.py / instruments.py)
# ---------------------------------------------------------------------------


def _utc_iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _request_id() -> str:
    return str(uuid.uuid4())


def _success_envelope(data: Any, *, data_source_type: str = "DERIVED") -> dict[str, Any]:
    return {
        "data": data,
        "metadata": {
            "requestedAt": _utc_iso_now(),
            "dataSourceType": data_source_type,
        },
    }


def _error_envelope(
    code: str,
    message: str,
    *,
    request_id: Optional[str] = None,
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
        content=json.dumps(content),
        status_code=status_code,
        media_type="application/json",
    )


# ---------------------------------------------------------------------------
# POST /v1/quality/evaluate
# ---------------------------------------------------------------------------


@router.post(
    "/quality/evaluate",
    summary="Evaluate DataQualityGate for a market data observation",
    description=(
        "Accepts a JSON body representing a market data observation and runs "
        "the DataQualityGate's five-condition check together with the "
        "QualityEngine's quality classification.  "
        "Returns gate conditions, classification (grade/signalEngineAllowed), "
        "and the full canonical metadata envelope.  "
        "(Requirements 7.2, 7.5, 7.6, 7.9)"
    ),
    response_class=Response,
)
async def evaluate_quality_gate(
    request: Request,
    body: Annotated[
        dict[str, Any],
        Body(
            description=(
                "Market data observation dict.  "
                "Recognised keys include: symbol, timestamp, open, high, low, close, "
                "volume, eventTimeMs, quoteAgeMs, freshnessFreshMs, providerAvailable, "
                "source, provider, confidenceScore.  "
                "See design.md §Quality Engine for the full field reference."
            ),
            examples=[
                {
                    "summary": "Healthy equity quote",
                    "value": {
                        "symbol": "RELIANCE",
                        "timestamp": 1705300000000,
                        "open": 2450.0,
                        "high": 2460.0,
                        "low": 2440.0,
                        "close": 2455.0,
                        "volume": 1000000,
                        "source": "angel_one",
                        "confidenceScore": 85,
                    },
                }
            ],
        ),
    ],
    min_confidence_score: Annotated[
        float,
        Query(
            description=(
                "Minimum DataConfidenceScore required to pass the confidence gate "
                "condition (dataSemanticallyValid). Default: 60. Range: 30–95."
            ),
            ge=30.0,
            le=95.0,
        ),
    ] = 60.0,
) -> Response:
    """Evaluate DataQualityGate and classify quality for a market data observation.

    Request body is the market data observation dict.  All fields are optional
    but the gate conditions will only pass when the relevant fields are present
    and valid.

    Response ``data`` shape::

        {
          "gate": {
            "dataFresh": bool,
            "dataComplete": bool,
            "dataTimestampValid": bool,
            "dataProviderHealthy": bool,
            "dataSemanticallyValid": bool,
            "signalEngineAllowed": bool,
            "confidenceScore": int,
            "blockReasons": [str, ...],
            "gates": { ... }
          },
          "classification": {
            "grade": str,           // HIGH | MEDIUM | LOW | BLOCKED
            "signalEngineAllowed": bool,
            "score": int,
            "reasons": [str, ...]
          },
          "signalEngineAllowed": bool   // top-level convenience field
        }
    """
    req_id = _request_id()

    if not isinstance(body, dict):
        return _json_response(
            _error_envelope(
                "INVALID_REQUEST_BODY",
                "Request body must be a JSON object (dict).",
                request_id=req_id,
            ),
            status_code=400,
        )

    try:
        gate_result = _engine.evaluate_gate(
            body,
            min_confidence_score=min_confidence_score,
        )
    except Exception as exc:  # noqa: BLE001
        _log.warning("quality_gate_evaluation_error", error=str(exc))
        return _json_response(
            _error_envelope(
                "EVALUATION_ERROR",
                f"Failed to evaluate quality gate: {exc}",
                request_id=req_id,
            ),
            status_code=500,
        )

    try:
        classification = _engine.classify_quality(
            score=int(gate_result.score),
            gate_result=gate_result,
        )
    except Exception as exc:  # noqa: BLE001
        _log.warning("quality_classification_error", error=str(exc))
        return _json_response(
            _error_envelope(
                "CLASSIFICATION_ERROR",
                f"Failed to classify quality: {exc}",
                request_id=req_id,
            ),
            status_code=500,
        )

    # Build the gate breakdown dict from the full DataQualityGate model when
    # available; fall back to the GateResult fields when it is not attached.
    gate_dict: dict[str, Any]
    if gate_result.gate is not None:
        gate_dict = gate_result.gate.model_dump()
    else:
        gate_dict = {
            "signalEngineAllowed": gate_result.passed,
            "confidenceScore": int(gate_result.score),
            "blockReasons": gate_result.failed_conditions,
        }

    payload: dict[str, Any] = {
        "gate": gate_dict,
        "classification": classification.model_dump(),
        "signalEngineAllowed": gate_result.passed,
    }

    return _json_response(_success_envelope(payload))


# ---------------------------------------------------------------------------
# GET /v1/quality/score
# ---------------------------------------------------------------------------


@router.get(
    "/quality/score",
    summary="Compute DataConfidenceScore from individual quality inputs",
    description=(
        "Computes a DataConfidenceScore from the supplied quality-input parameters "
        "without requiring a full observation dict.  Useful for dashboards and "
        "diagnostic tools that want to understand how each component affects the "
        "overall score.  "
        "(Requirements 7.1, 7.7)"
    ),
    response_class=Response,
)
async def get_quality_score(
    request: Request,
    freshness: Annotated[
        str,
        Query(
            description=(
                "Freshness classification. "
                "One of: FRESH, AGING, STALE, EXPIRED, UNKNOWN."
            )
        ),
    ],
    completeness: Annotated[
        float,
        Query(
            description="Completeness percentage, 0–100.",
            ge=0.0,
            le=100.0,
        ),
    ],
    provider_healthy: Annotated[
        bool,
        Query(
            description="Whether the data provider is considered healthy.",
        ),
    ],
    timestamp_valid: Annotated[
        bool,
        Query(
            description="Whether the data timestamp passed validation.",
        ),
    ],
    agreement: Annotated[
        float,
        Query(
            description=(
                "Cross-source agreement ratio, 0.0–1.0.  "
                "1.0 means full agreement across all providers."
            ),
            ge=0.0,
            le=1.0,
        ),
    ],
    sequence_ok: Annotated[
        bool,
        Query(
            description=(
                "Whether sequence integrity is intact.  "
                "False applies a 20%% score penalty."
            ),
        ),
    ] = True,
) -> Response:
    """Compute a DataConfidenceScore from individual quality-input parameters.

    Response ``data`` shape::

        {
          "score": int,               // DataConfidenceScore in [0, 95]
          "grade": str,               // HIGH | MEDIUM | LOW | BLOCKED
          "signalEngineAllowed": bool,
          "components": {
            "freshness": str,         // the freshness classification used
            "freshnessScore": int,    // 0–35
            "completenessScore": int, // 0–25
            "providerHealthScore": int, // 0 or 20
            "timestampScore": int,    // 0 or 10
            "agreementScore": int,    // 0–10
            "sequencePenaltyApplied": bool
          }
        }
    """
    req_id = _request_id()

    # Validate freshness classification
    freshness_upper = freshness.upper()
    if freshness_upper not in _VALID_FRESHNESS:
        return _json_response(
            _error_envelope(
                "INVALID_PARAMETER",
                f"Invalid freshness '{freshness}'. "
                f"Valid values: {sorted(_VALID_FRESHNESS)}.",
                request_id=req_id,
            ),
            status_code=400,
        )

    try:
        score = QualityEngine.compute_confidence_from_inputs(
            freshness_classification=freshness_upper,
            completeness_percent=completeness,
            provider_healthy=provider_healthy,
            timestamp_valid=timestamp_valid,
            cross_source_agreement=agreement,
            sequence_integrity_ok=sequence_ok,
        )
    except Exception as exc:  # noqa: BLE001
        _log.warning("quality_score_computation_error", error=str(exc))
        return _json_response(
            _error_envelope(
                "COMPUTATION_ERROR",
                f"Failed to compute quality score: {exc}",
                request_id=req_id,
            ),
            status_code=500,
        )

    grade = QualityEngine.grade_score(score)
    signal_allowed = QualityEngine.is_signal_engine_allowed(score)

    # Compute component breakdown for transparency
    from src.engines.quality_engine import _FRESHNESS_SCORE  # noqa: PLC0415

    freshness_score = _FRESHNESS_SCORE.get(freshness_upper, _FRESHNESS_SCORE["UNKNOWN"])
    completeness_score = int(max(0.0, min(completeness, 100.0)) * 0.25)
    provider_health_score = 20 if provider_healthy else 0
    timestamp_score = 10 if timestamp_valid else 0
    agreement_score = int(agreement * 10)
    penalty_applied = not sequence_ok

    payload: dict[str, Any] = {
        "score": score,
        "grade": grade,
        "signalEngineAllowed": signal_allowed,
        "components": {
            "freshness": freshness_upper,
            "freshnessScore": freshness_score,
            "completenessScore": completeness_score,
            "providerHealthScore": provider_health_score,
            "timestampScore": timestamp_score,
            "agreementScore": agreement_score,
            "sequencePenaltyApplied": penalty_applied,
        },
    }

    return _json_response(_success_envelope(payload))
