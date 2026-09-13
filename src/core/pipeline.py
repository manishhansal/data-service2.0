"""
14-step Validation Pipeline for DATA-SERVICE 2.0.

Every dataset received from an external provider must traverse all 14 steps
in order before it can be served to consumers or persisted to the database.
A dataset that fails at any step does NOT advance to subsequent steps.

Pipeline steps (in order):
    1.  raw_receipt        — accept raw provider response
    2.  schema_validate    — JSON schema validation; reject + DataIncident on failure
    3.  normalise          — map to canonical Pydantic schema
    4.  timestamp_normalise — convert all timestamps to UTC epoch ms
    5.  semantic_validate  — OI/volume/IV/bid-ask integrity
    6.  dedup              — duplicate detection via deterministic hash
    7.  gap_detect         — gap detection vs expected candle sequence
    8.  freshness_classify — classify as FRESH/AGING/STALE/EXPIRED/UNKNOWN
    9.  reconcile          — cross-provider reconciliation (CONFIRMED/MINOR/MAJOR)
    10. quality_score      — DataConfidenceScore + DataQualityGate
    11. canonical_output   — build the final canonical dataset
    12. cache_populate     — write to L2 Redis + L1 in-process
    13. persist            — write to L3 PostgreSQL
    14. deliver            — API / Event_Bus delivery

Design rules
------------
- ``ValidationPipeline.run(raw_dataset)`` runs all non-skipped steps in order
  and returns a ``PipelineResult`` describing the outcome.
- Each step returns ``(dataset, ok, incident)``; if ``ok`` is ``False`` the
  pipeline stops and records the failing step.
- POOR_QUALITY rule: datasets with DataConfidenceScore < 60 after step 10
  are flagged ``poor_quality``.  They are still persisted (step 13) but are
  NOT delivered via API or Event Bus (step 14 is suppressed).
- Step-skip policy: no step may be skipped without an explicit OVERRIDE_REASON
  (1–500 characters) and a high-severity alert emitted within 5 seconds
  (Requirement 17.2).  Calling ``skip_step`` without a reason raises
  ``ValueError``.

Note: all step methods are **stubs** at this stage — they pass data through
without real logic.  Real logic is added in tasks 5.2–5.10.

Requirements: 17.1, 17.2, 5.10
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

import structlog

from src.observability.logging import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Steps available for skipping (must match method names below)
PIPELINE_STEP_NAMES: tuple[str, ...] = (
    "raw_receipt",
    "schema_validate",
    "normalise",
    "timestamp_normalise",
    "semantic_validate",
    "dedup",
    "gap_detect",
    "freshness_classify",
    "reconcile",
    "quality_score",
    "canonical_output",
    "cache_populate",
    "persist",
    "deliver",
)

# DataConfidenceScore threshold below which a dataset is flagged POOR_QUALITY.
# POOR_QUALITY datasets are persisted but not delivered (Requirement 17.7).
POOR_QUALITY_SCORE_THRESHOLD: int = 60

# Valid range for OVERRIDE_REASON string (characters)
_OVERRIDE_REASON_MIN: int = 1
_OVERRIDE_REASON_MAX: int = 500

# ---------------------------------------------------------------------------
# DataIncident — minimal schema (full schema in Task 9 / schemas/incident.py)
# ---------------------------------------------------------------------------


@dataclass
class DataIncident:
    """Minimal DataIncident record used during pipeline execution.

    This is a lightweight dataclass placeholder.  The full Pydantic-based
    schema (with database persistence) is defined in Task 9.

    Fields
    ------
    incidentId    : UUID v4 string assigned at creation.
    incidentType  : Category string, e.g. 'SCHEMA_VALIDATION' or 'OHLC_INVARIANT'.
    instrumentId  : Canonical instrument identifier.
    provider      : Source provider identifier.
    timestamp     : UTC ISO-8601 string of when the incident was detected.
    severity      : 'LOW' | 'MEDIUM' | 'HIGH' | 'CRITICAL'.
    details       : Free-form dict with incident-specific data.
    """

    incidentId: str
    incidentType: str
    instrumentId: str
    provider: str
    timestamp: str
    severity: str
    details: dict = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        *,
        incident_type: str,
        instrument_id: str,
        provider: str,
        severity: str = "HIGH",
        details: Optional[dict] = None,
    ) -> "DataIncident":
        """Convenience factory that auto-assigns incidentId and timestamp."""
        import datetime

        return cls(
            incidentId=str(uuid.uuid4()),
            incidentType=incident_type,
            instrumentId=instrument_id,
            provider=provider,
            timestamp=datetime.datetime.now(datetime.timezone.utc).isoformat(),
            severity=severity,
            details=details or {},
        )


# ---------------------------------------------------------------------------
# StepSkipEntry — recorded in the audit log when a step is skipped
# ---------------------------------------------------------------------------


@dataclass
class StepSkipEntry:
    """Audit record for a skipped pipeline step.

    Required by Requirement 17.2: any configuration that disables a pipeline
    step must produce an audit entry with an OVERRIDE_REASON and a
    high-severity alert within 5 seconds.
    """

    step_name: str
    override_reason: str
    skipped_at: float  # time.monotonic() value
    audit_id: str = field(default_factory=lambda: str(uuid.uuid4()))


# ---------------------------------------------------------------------------
# PipelineResult
# ---------------------------------------------------------------------------


@dataclass
class PipelineResult:
    """Outcome of a single ``ValidationPipeline.run()`` invocation.

    Attributes
    ----------
    ok              : True when all executed steps passed (pipeline completed
                      successfully and the dataset was delivered).
    dataset         : The dataset dict as it was at the end of the last
                      successful step (None if pipeline failed at step 1 or
                      before any mutation).
    step_failed     : Name of the step that caused failure, or None on success.
    incident        : DataIncident produced by the failing step, or None.
    steps_completed : Ordered list of step names that were executed and passed.
    poor_quality    : True when DataConfidenceScore < 60 (dataset persisted
                      but not delivered — step 14 suppressed).
    """

    ok: bool
    dataset: Optional[dict]
    step_failed: Optional[str]
    incident: Optional[DataIncident]
    steps_completed: list[str]
    poor_quality: bool


# ---------------------------------------------------------------------------
# ValidationPipeline
# ---------------------------------------------------------------------------


class ValidationPipeline:
    """Ordered 14-step validation and delivery pipeline.

    Instantiate once per application lifetime (or per test run). The pipeline
    maintains an internal set of skipped steps with their audit records.

    Parameters
    ----------
    skip_alert_fn : Optional callable ``(step_name: str, reason: str) -> None``
        Injected callback for emitting a high-severity alert when a step is
        skipped. Defaults to a structlog WARN call. Tests inject a callable
        that records calls instead of sending real alerts.
    """

    def __init__(
        self,
        skip_alert_fn: Optional[Any] = None,
    ) -> None:
        # Maps step_name → StepSkipEntry when a step has been skipped.
        self._skipped_steps: dict[str, StepSkipEntry] = {}

        # Alert callback — injected for testability; defaults to structlog warn.
        self._skip_alert_fn = skip_alert_fn or self._default_skip_alert

    # ------------------------------------------------------------------ #
    # Public interface                                                      #
    # ------------------------------------------------------------------ #

    def skip_step(self, step_name: str, override_reason: str) -> StepSkipEntry:
        """Register a step as skipped with a mandatory OVERRIDE_REASON.

        Rules (Requirement 17.2):
        - ``step_name`` must be a valid pipeline step name.
        - ``override_reason`` must be 1–500 non-whitespace-stripped characters.
        - A high-severity alert is emitted via ``skip_alert_fn`` within this
          call (synchronous, target <5 seconds).
        - The skip is recorded in an internal audit log; the entry is returned
          for callers that wish to record it externally.

        Raises
        ------
        ValueError
            If ``step_name`` is not a valid step, or if ``override_reason`` is
            empty or exceeds 500 characters.
        """
        if step_name not in PIPELINE_STEP_NAMES:
            raise ValueError(
                f"Unknown pipeline step: {step_name!r}. "
                f"Valid steps are: {PIPELINE_STEP_NAMES}"
            )

        reason_stripped = override_reason.strip() if override_reason else ""
        if not reason_stripped:
            raise ValueError(
                f"override_reason must not be empty (Requirement 17.2). "
                f"Provide a reason of 1–{_OVERRIDE_REASON_MAX} characters explaining "
                f"why step {step_name!r} is being skipped."
            )
        if len(reason_stripped) > _OVERRIDE_REASON_MAX:
            raise ValueError(
                f"override_reason exceeds {_OVERRIDE_REASON_MAX} characters "
                f"(got {len(reason_stripped)}) for step {step_name!r}."
            )

        entry = StepSkipEntry(
            step_name=step_name,
            override_reason=reason_stripped,
            skipped_at=time.monotonic(),
        )
        self._skipped_steps[step_name] = entry

        # Emit a high-severity alert immediately (Requirement 17.2)
        self._skip_alert_fn(step_name, reason_stripped)

        logger.warning(
            "pipeline_step_skipped",
            component="validation_pipeline",
            step=step_name,
            override_reason=reason_stripped,
            audit_id=entry.audit_id,
        )
        return entry

    @property
    def skipped_steps(self) -> dict[str, StepSkipEntry]:
        """Return a copy of the current skip audit log."""
        return dict(self._skipped_steps)

    def run(self, raw_dataset: dict) -> PipelineResult:
        """Execute all 14 pipeline steps in order.

        Parameters
        ----------
        raw_dataset : The raw provider response as a plain Python dict.

        Returns
        -------
        PipelineResult describing the final outcome.

        Behaviour
        ---------
        - Steps registered via ``skip_step`` are bypassed; the dataset is
          passed through unchanged and the step is *not* added to
          ``steps_completed``.
        - The first step returning ``ok=False`` stops the pipeline; subsequent
          steps are not called.
        - After step 10 (quality_score), if ``dataset.get("_confidenceScore",
          100) < POOR_QUALITY_SCORE_THRESHOLD``, the dataset is flagged
          ``poor_quality = True``.  Step 14 (deliver) is then suppressed.
        """
        dataset: dict = dict(raw_dataset)  # shallow copy — steps may mutate
        steps_completed: list[str] = []
        poor_quality: bool = False

        # Ordered list of (step_name, method)
        ordered_steps: list[tuple[str, Any]] = [
            ("raw_receipt",         self._step_raw_receipt),
            ("schema_validate",     self._step_schema_validate),
            ("normalise",           self._step_normalise),
            ("timestamp_normalise", self._step_timestamp_normalise),
            ("semantic_validate",   self._step_semantic_validate),
            ("dedup",               self._step_dedup),
            ("gap_detect",          self._step_gap_detect),
            ("freshness_classify",  self._step_freshness_classify),
            ("reconcile",           self._step_reconcile),
            ("quality_score",       self._step_quality_score),
            ("canonical_output",    self._step_canonical_output),
            ("cache_populate",      self._step_cache_populate),
            ("persist",             self._step_persist),
            ("deliver",             self._step_deliver),
        ]

        for step_name, step_fn in ordered_steps:
            # ── Step-skip policy ──────────────────────────────────────────
            if step_name in self._skipped_steps:
                # Dataset passes through unchanged; step is not recorded in
                # steps_completed (skipped steps are audited separately).
                continue

            # ── POOR_QUALITY suppression of delivery ──────────────────────
            if step_name == "deliver" and poor_quality:
                # Persist ran (step 13); delivery is suppressed per 17.7.
                logger.info(
                    "pipeline_delivery_suppressed",
                    component="validation_pipeline",
                    reason="poor_quality",
                    instrument_id=dataset.get("instrumentId"),
                )
                # step NOT added to steps_completed; result is ok=True with
                # poor_quality=True.
                break

            # ── Execute step ──────────────────────────────────────────────
            dataset, ok, incident = step_fn(dataset)

            if ok:
                steps_completed.append(step_name)
            else:
                logger.warning(
                    "pipeline_step_failed",
                    component="validation_pipeline",
                    step=step_name,
                    instrument_id=dataset.get("instrumentId"),
                    incident_id=incident.incidentId if incident else None,
                )
                return PipelineResult(
                    ok=False,
                    dataset=dataset,
                    step_failed=step_name,
                    incident=incident,
                    steps_completed=steps_completed,
                    poor_quality=False,
                )

            # ── POOR_QUALITY detection after quality_score (step 10) ─────
            if step_name == "quality_score":
                confidence_score = dataset.get("_confidenceScore")
                if (
                    confidence_score is not None
                    and isinstance(confidence_score, (int, float))
                ):
                    dataset, poor_quality = self._apply_poor_quality_rule(
                        dataset, int(confidence_score)
                    )

        return PipelineResult(
            ok=True,
            dataset=dataset,
            step_failed=None,
            incident=None,
            steps_completed=steps_completed,
            poor_quality=poor_quality,
        )

    # ------------------------------------------------------------------ #
    # Private: poor-quality rule helper                                     #
    # ------------------------------------------------------------------ #

    def _apply_poor_quality_rule(
        self, dataset: dict, score: int
    ) -> tuple[dict, bool]:
        """Apply the POOR_QUALITY rule after step 10 (quality_score).

        A dataset with ``DataConfidenceScore < POOR_QUALITY_SCORE_THRESHOLD``
        is flagged ``POOR_QUALITY``:
        - ``dataset["_poorQuality"]`` is set to ``True``
        - The dataset is still persisted (step 13) but NOT delivered (step 14)

        Parameters
        ----------
        dataset : The current pipeline dataset dict (mutated in-place).
        score   : The computed DataConfidenceScore integer.

        Returns
        -------
        ``(dataset, is_poor_quality)`` — the updated dataset and a bool
        indicating whether the poor-quality flag was applied.

        Requirements: 17.7
        """
        if score < POOR_QUALITY_SCORE_THRESHOLD:
            dataset["_poorQuality"] = True
            logger.info(
                "pipeline_poor_quality_flagged",
                component="validation_pipeline",
                confidence_score=score,
                threshold=POOR_QUALITY_SCORE_THRESHOLD,
                instrument_id=dataset.get("instrumentId"),
            )
            return dataset, True
        return dataset, False

    # ------------------------------------------------------------------ #
    # Private: default skip alert                                           #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _default_skip_alert(step_name: str, override_reason: str) -> None:
        """Default high-severity alert emitted when a step is skipped.

        In production this would send an alert to PagerDuty / OpsGenie or
        push to the observability pipeline.  For the skeleton it emits a
        structured CRITICAL log that satisfies the "within 5 seconds"
        requirement synchronously (Requirement 17.2).
        """
        _alert_logger = structlog.get_logger("pipeline.alert")
        _alert_logger.critical(
            "pipeline_step_skip_alert",
            component="validation_pipeline",
            alert_type="PIPELINE_STEP_SKIPPED",
            severity="CRITICAL",
            step=step_name,
            override_reason=override_reason,
        )

    # ------------------------------------------------------------------ #
    # Pipeline step stubs                                                   #
    # ------------------------------------------------------------------ #
    #
    # Each stub accepts the current ``dataset`` dict and returns a tuple:
    #   (dataset, ok: bool, incident: Optional[DataIncident])
    #
    # Returning ``ok=False`` stops the pipeline.  The stub implementations
    # here simply pass data through — real logic is added in tasks 5.2–5.10.

    def _step_raw_receipt(
        self, dataset: dict
    ) -> tuple[dict, bool, Optional[DataIncident]]:
        """Step 1: Accept raw provider response.

        Real implementation (Task 5.2+): assigns dataObservationId UUID,
        records receivedAtMs, performs provider-level health check.
        """
        return dataset, True, None

    def _step_schema_validate(
        self, dataset: dict
    ) -> tuple[dict, bool, Optional[DataIncident]]:
        """Step 2: JSON schema validation.

        Real implementation (Task 5.2): validates the raw provider response
        against the expected JSON schema.  On failure: creates a DataIncident
        (SCHEMA_VALIDATION, HIGH), logs the error, and returns ok=False.

        Requirement 17.6: on failure, record provider, endpoint,
        rawResponseHash, validationErrors, receivedAt.
        """
        return dataset, True, None

    def _step_normalise(
        self, dataset: dict
    ) -> tuple[dict, bool, Optional[DataIncident]]:
        """Step 3: Map provider-specific response to canonical Pydantic schema.

        Real implementation (Task 5.2): invokes the Normaliser, enforces all
        null semantics (oi, iv, Greeks, bid/ask), attaches normalisationVersion.
        """
        return dataset, True, None

    def _step_timestamp_normalise(
        self, dataset: dict
    ) -> tuple[dict, bool, Optional[DataIncident]]:
        """Step 4: Convert all timestamps to UTC epoch milliseconds.

        Real implementation (Task 5.4): uses ZoneInfo("Asia/Kolkata") for
        Indian markets, UTC for crypto.  Rejects unparseable timestamps with
        a DataIncident.

        Requirements: 6.7, 6.8, 17.3
        """
        return dataset, True, None

    def _step_semantic_validate(
        self, dataset: dict
    ) -> tuple[dict, bool, Optional[DataIncident]]:
        """Step 5: Semantic validation.

        Real implementation (Task 5.5): enforces OI≠tradedValue substitution,
        IV not zero-substituted, Greeks not zero-substituted, bid/ask not
        zero-substituted.

        Requirements: 6.2, 6.3, 6.4, 6.5, 6.6, 3.3
        """
        return dataset, True, None

    def _step_dedup(
        self, dataset: dict
    ) -> tuple[dict, bool, Optional[DataIncident]]:
        """Step 6: Duplicate detection via deterministic hash.

        Real implementation (Task 5.6): computes
        SHA-256(instrumentId:eventTimeMs:source:ltp:volume)[:32].
        Duplicates: sets isDuplicate=True, does NOT persist, counts in
        quality statistics.

        Requirements: 3.6, 17.4
        """
        return dataset, True, None

    def _step_gap_detect(
        self, dataset: dict
    ) -> tuple[dict, bool, Optional[DataIncident]]:
        """Step 7: Gap detection vs expected candle sequence.

        Real implementation (Task 5.7): emits DataGapEvent records with
        gapStartMs, gapEndMs, expectedCount, actualCount, severity
        (LOW=1, MEDIUM=2-5, HIGH>5).

        Requirement: 17.5
        """
        return dataset, True, None

    def _step_freshness_classify(
        self, dataset: dict
    ) -> tuple[dict, bool, Optional[DataIncident]]:
        """Step 8: Classify data freshness per instrument tier and session phase.

        Real implementation (Task 6+): returns FRESH/AGING/STALE/EXPIRED/UNKNOWN
        based on eventTimeMs age vs thresholds from the Quality Engine.

        Requirements: 7.3, 7.4
        """
        return dataset, True, None

    def _step_reconcile(
        self, dataset: dict
    ) -> tuple[dict, bool, Optional[DataIncident]]:
        """Step 9: Cross-provider reconciliation.

        Real implementation (Task 7.2): computes deviation between providers
        for the same (instrumentId, exchange, interval, timestamp) tuple.
        CONFIRMED / MINOR_DISCREPANCY / MAJOR_DISCREPANCY.

        Requirements: 10.5, 10.6, 10.7
        """
        return dataset, True, None

    def _step_quality_score(
        self, dataset: dict
    ) -> tuple[dict, bool, Optional[DataIncident]]:
        """Step 10: Compute DataConfidenceScore + DataQualityGate.

        Real implementation (Task 9): computes the weighted score [0, 95],
        evaluates the five-condition DataQualityGate, flags POOR_QUALITY
        when score < 60.

        Requirements: 7.1, 7.2, 17.7
        """
        return dataset, True, None

    def _step_canonical_output(
        self, dataset: dict
    ) -> tuple[dict, bool, Optional[DataIncident]]:
        """Step 11: Build the final canonical dataset response envelope.

        Real implementation (Task 5.2+): constructs the full canonical
        success envelope with data, metadata, provenance, quality.
        """
        return dataset, True, None

    def _step_cache_populate(
        self, dataset: dict
    ) -> tuple[dict, bool, Optional[DataIncident]]:
        """Step 12: Write to L2 Redis and L1 in-process cache.

        Real implementation (Task 2.6): applies correct TTLs, uses the
        mds: key namespace, sets dataSourceType=CACHED on cache hits.

        Requirements: 9.1, 9.2, 9.3, 9.9
        """
        return dataset, True, None

    def _step_persist(
        self, dataset: dict
    ) -> tuple[dict, bool, Optional[DataIncident]]:
        """Step 13: Persist to L3 PostgreSQL.

        Real implementation (Task 2.1+): bulk-upserts candle_bar rows,
        writes data_provenance record within 1000ms.

        Requirements: 8.6, 4.5
        """
        return dataset, True, None

    def _step_deliver(
        self, dataset: dict
    ) -> tuple[dict, bool, Optional[DataIncident]]:
        """Step 14: Deliver via REST API response and/or Event Bus.

        Suppressed when ``poor_quality=True`` (dataset persisted but not
        delivered — Requirement 17.7).

        Real implementation (Task 8+): publishes to the Event Bus stream,
        resolves waiting API responses.
        """
        return dataset, True, None
