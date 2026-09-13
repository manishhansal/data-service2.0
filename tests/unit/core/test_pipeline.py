"""
Unit tests for the 14-step ValidationPipeline skeleton.

Covers:
- All 14 steps run in order on a successful pass
- Failed step stops pipeline advancement; subsequent steps are not executed
- POOR_QUALITY flag is set when DataConfidenceScore < 60 after step 10
- Step skip requires a non-empty OVERRIDE_REASON or raises ValueError
- Skip without valid step name raises ValueError
- Override reason length boundaries (0 chars, 1 char, 500 chars, 501 chars)
- PipelineResult fields are correctly populated for success, failure, and
  poor-quality paths
- Skipped steps are not recorded in steps_completed
- POOR_QUALITY datasets: persist (step 13) runs, deliver (step 14) is suppressed

Requirements: 17.1, 17.2, 5.10
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

import pytest

from src.core.pipeline import (
    PIPELINE_STEP_NAMES,
    POOR_QUALITY_SCORE_THRESHOLD,
    DataIncident,
    PipelineResult,
    StepSkipEntry,
    ValidationPipeline,
)


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_dataset(**kwargs: Any) -> dict:
    """Return a minimal dataset dict suitable for pipeline input."""
    defaults: dict = {
        "instrumentId": "NSE:NIFTY:IDX",
        "provider": "scrapling_nse",
        "ltp": 22150.50,
    }
    defaults.update(kwargs)
    return defaults


def _capture_alerts() -> tuple[list[tuple[str, str]], Any]:
    """Return (captured_list, alert_fn) for injection into ValidationPipeline."""
    captured: list[tuple[str, str]] = []

    def _alert(step_name: str, reason: str) -> None:
        captured.append((step_name, reason))

    return captured, _alert


def _pipeline_with_capture() -> tuple[ValidationPipeline, list[tuple[str, str]]]:
    captured, alert_fn = _capture_alerts()
    pipe = ValidationPipeline(skip_alert_fn=alert_fn)
    return pipe, captured


# ---------------------------------------------------------------------------
# DataIncident tests
# ---------------------------------------------------------------------------


class TestDataIncident:
    def test_create_factory_assigns_uuid_and_timestamp(self) -> None:
        incident = DataIncident.create(
            incident_type="SCHEMA_VALIDATION",
            instrument_id="NSE:RELIANCE:EQ",
            provider="angel_one",
        )
        assert incident.incidentId
        assert len(incident.incidentId) == 36  # UUID v4 string length
        assert "T" in incident.timestamp  # ISO-8601

    def test_create_factory_defaults_severity_to_high(self) -> None:
        incident = DataIncident.create(
            incident_type="OHLC_INVARIANT",
            instrument_id="NSE:RELIANCE:EQ",
            provider="angel_one",
        )
        assert incident.severity == "HIGH"

    def test_create_factory_accepts_custom_severity(self) -> None:
        incident = DataIncident.create(
            incident_type="DUPLICATE",
            instrument_id="NSE:NIFTY:IDX",
            provider="scrapling_nse",
            severity="LOW",
        )
        assert incident.severity == "LOW"

    def test_create_factory_accepts_details(self) -> None:
        details = {"failedInvariant": "high < close", "rejectedValues": {"high": 100, "close": 110}}
        incident = DataIncident.create(
            incident_type="OHLC_INVARIANT",
            instrument_id="NSE:NIFTY:IDX",
            provider="upstox",
            details=details,
        )
        assert incident.details == details

    def test_default_details_is_empty_dict(self) -> None:
        incident = DataIncident.create(
            incident_type="GAP_DETECTED",
            instrument_id="NSE:NIFTY:IDX",
            provider="openchart",
        )
        assert incident.details == {}


# ---------------------------------------------------------------------------
# PipelineResult tests
# ---------------------------------------------------------------------------


class TestPipelineResult:
    def test_ok_result_attributes(self) -> None:
        result = PipelineResult(
            ok=True,
            dataset={"key": "val"},
            step_failed=None,
            incident=None,
            steps_completed=["raw_receipt"],
            poor_quality=False,
        )
        assert result.ok is True
        assert result.step_failed is None
        assert result.incident is None
        assert result.poor_quality is False

    def test_failed_result_attributes(self) -> None:
        incident = DataIncident.create(
            incident_type="SCHEMA_VALIDATION",
            instrument_id="NSE:X:EQ",
            provider="angel_one",
        )
        result = PipelineResult(
            ok=False,
            dataset={"key": "val"},
            step_failed="schema_validate",
            incident=incident,
            steps_completed=["raw_receipt"],
            poor_quality=False,
        )
        assert result.ok is False
        assert result.step_failed == "schema_validate"
        assert result.incident is incident


# ---------------------------------------------------------------------------
# ValidationPipeline — happy path (all 14 steps pass as stubs)
# ---------------------------------------------------------------------------


class TestValidationPipelineHappyPath:
    def test_all_14_steps_run_in_order_on_success(self) -> None:
        pipe = ValidationPipeline()
        raw = _make_dataset()
        result = pipe.run(raw)

        assert result.ok is True
        assert result.step_failed is None
        assert result.incident is None
        assert result.poor_quality is False
        # All 14 steps should be in steps_completed, in order
        assert result.steps_completed == list(PIPELINE_STEP_NAMES)

    def test_result_dataset_contains_input_data(self) -> None:
        pipe = ValidationPipeline()
        raw = _make_dataset(ltp=999.0)
        result = pipe.run(raw)
        assert result.dataset is not None
        assert result.dataset["ltp"] == 999.0

    def test_steps_completed_are_ordered(self) -> None:
        pipe = ValidationPipeline()
        result = pipe.run(_make_dataset())
        expected_order = list(PIPELINE_STEP_NAMES)
        assert result.steps_completed == expected_order

    def test_run_does_not_mutate_original_dict(self) -> None:
        """Pipeline should not modify the caller's original dict."""
        pipe = ValidationPipeline()
        raw = _make_dataset()
        original_id = id(raw)
        pipe.run(raw)
        # The object identity of the input dict is unchanged
        assert id(raw) == original_id

    def test_result_has_no_incident_on_success(self) -> None:
        pipe = ValidationPipeline()
        result = pipe.run(_make_dataset())
        assert result.incident is None


# ---------------------------------------------------------------------------
# ValidationPipeline — failure path
# ---------------------------------------------------------------------------


class TestValidationPipelineFailure:
    def _pipeline_that_fails_at(self, step_name: str) -> ValidationPipeline:
        """Return a pipeline whose named step always returns (dataset, False, incident)."""
        pipe = ValidationPipeline()
        incident = DataIncident.create(
            incident_type="TEST_FAILURE",
            instrument_id="NSE:TEST:EQ",
            provider="test_provider",
        )

        def failing_step(dataset: dict) -> tuple[dict, bool, Optional[DataIncident]]:
            return dataset, False, incident

        # Patch the private method
        setattr(pipe, f"_step_{step_name}", failing_step)
        return pipe

    @pytest.mark.parametrize("failing_step", PIPELINE_STEP_NAMES)
    def test_failed_step_stops_pipeline(self, failing_step: str) -> None:
        pipe = self._pipeline_that_fails_at(failing_step)
        result = pipe.run(_make_dataset())

        assert result.ok is False
        assert result.step_failed == failing_step

    @pytest.mark.parametrize("failing_step", PIPELINE_STEP_NAMES)
    def test_steps_after_failure_are_not_executed(self, failing_step: str) -> None:
        pipe = self._pipeline_that_fails_at(failing_step)
        result = pipe.run(_make_dataset())

        failing_index = PIPELINE_STEP_NAMES.index(failing_step)
        # Steps before the failing step should be completed
        expected_completed = list(PIPELINE_STEP_NAMES[:failing_index])
        assert result.steps_completed == expected_completed

    def test_incident_is_captured_in_result(self) -> None:
        pipe = self._pipeline_that_fails_at("schema_validate")
        result = pipe.run(_make_dataset())

        assert result.incident is not None
        assert result.incident.incidentType == "TEST_FAILURE"

    def test_result_dataset_is_dataset_at_point_of_failure(self) -> None:
        """dataset in PipelineResult should be the state at the failing step."""
        pipe = ValidationPipeline()
        incident = DataIncident.create(
            incident_type="SCHEMA_VALIDATION",
            instrument_id="NSE:X:EQ",
            provider="test",
        )
        captured_dataset: dict = {}

        def failing_step(dataset: dict) -> tuple[dict, bool, Optional[DataIncident]]:
            nonlocal captured_dataset
            captured_dataset = dataset
            return dataset, False, incident

        setattr(pipe, "_step_schema_validate", failing_step)
        result = pipe.run(_make_dataset(ltp=12.34))

        assert result.dataset is not None
        assert result.dataset["ltp"] == 12.34


# ---------------------------------------------------------------------------
# ValidationPipeline — POOR_QUALITY path
# ---------------------------------------------------------------------------


class TestPoorQualityPath:
    def _pipeline_with_low_confidence(self, score: int) -> ValidationPipeline:
        """Return a pipeline whose quality_score step injects a low score."""
        pipe = ValidationPipeline()

        def quality_step(dataset: dict) -> tuple[dict, bool, Optional[DataIncident]]:
            dataset["_confidenceScore"] = score
            return dataset, True, None

        setattr(pipe, "_step_quality_score", quality_step)
        return pipe

    @pytest.mark.parametrize("score", [0, 1, 29, 59])
    def test_score_below_threshold_sets_poor_quality_flag(self, score: int) -> None:
        pipe = self._pipeline_with_low_confidence(score)
        result = pipe.run(_make_dataset())

        assert result.poor_quality is True

    @pytest.mark.parametrize("score", [60, 61, 79, 80, 95])
    def test_score_at_or_above_threshold_does_not_set_poor_quality(self, score: int) -> None:
        pipe = self._pipeline_with_low_confidence(score)
        result = pipe.run(_make_dataset())

        assert result.poor_quality is False

    def test_threshold_boundary_exactly_60_is_not_poor_quality(self) -> None:
        pipe = self._pipeline_with_low_confidence(POOR_QUALITY_SCORE_THRESHOLD)
        result = pipe.run(_make_dataset())
        assert result.poor_quality is False

    def test_threshold_boundary_59_is_poor_quality(self) -> None:
        pipe = self._pipeline_with_low_confidence(POOR_QUALITY_SCORE_THRESHOLD - 1)
        result = pipe.run(_make_dataset())
        assert result.poor_quality is True

    def test_poor_quality_dataset_marked_in_dataset_dict(self) -> None:
        pipe = self._pipeline_with_low_confidence(30)
        result = pipe.run(_make_dataset())

        assert result.dataset is not None
        assert result.dataset.get("_poorQuality") is True

    def test_poor_quality_persist_runs_but_deliver_is_suppressed(self) -> None:
        """Step 13 (persist) must run; step 14 (deliver) must not."""
        steps_called: list[str] = []

        pipe = ValidationPipeline()

        # Inject low confidence after quality_score
        original_quality = pipe._step_quality_score

        def quality_step(dataset: dict) -> tuple[dict, bool, Optional[DataIncident]]:
            dataset["_confidenceScore"] = 30
            return dataset, True, None

        def tracking_persist(dataset: dict) -> tuple[dict, bool, Optional[DataIncident]]:
            steps_called.append("persist")
            return dataset, True, None

        def tracking_deliver(dataset: dict) -> tuple[dict, bool, Optional[DataIncident]]:
            steps_called.append("deliver")
            return dataset, True, None

        pipe._step_quality_score = quality_step  # type: ignore[method-assign]
        pipe._step_persist = tracking_persist  # type: ignore[method-assign]
        pipe._step_deliver = tracking_deliver  # type: ignore[method-assign]

        result = pipe.run(_make_dataset())

        assert result.poor_quality is True
        assert result.ok is True
        assert "persist" in steps_called, "persist must run for poor_quality datasets"
        assert "deliver" not in steps_called, "deliver must NOT run for poor_quality datasets"

    def test_poor_quality_result_is_ok_true(self) -> None:
        """poor_quality is a quality flag, not a pipeline failure."""
        pipe = self._pipeline_with_low_confidence(20)
        result = pipe.run(_make_dataset())

        assert result.ok is True
        assert result.step_failed is None
        assert result.incident is None

    def test_deliver_not_in_steps_completed_when_poor_quality(self) -> None:
        pipe = self._pipeline_with_low_confidence(10)
        result = pipe.run(_make_dataset())

        assert "deliver" not in result.steps_completed

    def test_no_confidence_score_does_not_flag_poor_quality(self) -> None:
        """Dataset with no _confidenceScore key must NOT be flagged poor_quality."""
        pipe = ValidationPipeline()
        result = pipe.run(_make_dataset())

        assert result.poor_quality is False


# ---------------------------------------------------------------------------
# ValidationPipeline — step-skip policy
# ---------------------------------------------------------------------------


class TestStepSkipPolicy:
    def test_skip_step_requires_non_empty_override_reason(self) -> None:
        pipe, _ = _pipeline_with_capture()
        with pytest.raises(ValueError, match="override_reason must not be empty"):
            pipe.skip_step("dedup", "")

    def test_skip_step_requires_non_whitespace_override_reason(self) -> None:
        pipe, _ = _pipeline_with_capture()
        with pytest.raises(ValueError, match="override_reason must not be empty"):
            pipe.skip_step("dedup", "   ")

    def test_skip_step_raises_for_unknown_step(self) -> None:
        pipe, _ = _pipeline_with_capture()
        with pytest.raises(ValueError, match="Unknown pipeline step"):
            pipe.skip_step("nonexistent_step", "reason")

    def test_skip_step_raises_for_reason_exceeding_500_chars(self) -> None:
        pipe, _ = _pipeline_with_capture()
        long_reason = "x" * 501
        with pytest.raises(ValueError, match="exceeds 500 characters"):
            pipe.skip_step("dedup", long_reason)

    def test_skip_step_accepts_exactly_500_chars(self) -> None:
        pipe, _ = _pipeline_with_capture()
        reason = "x" * 500
        entry = pipe.skip_step("dedup", reason)
        assert entry.override_reason == reason

    def test_skip_step_accepts_exactly_1_char(self) -> None:
        pipe, _ = _pipeline_with_capture()
        entry = pipe.skip_step("gap_detect", "X")
        assert entry.override_reason == "X"

    def test_skip_step_returns_step_skip_entry(self) -> None:
        pipe, _ = _pipeline_with_capture()
        entry = pipe.skip_step("dedup", "Skipping dedup in back-fill mode")
        assert isinstance(entry, StepSkipEntry)
        assert entry.step_name == "dedup"
        assert entry.audit_id  # non-empty UUID

    def test_skip_step_records_entry_in_skipped_steps(self) -> None:
        pipe, _ = _pipeline_with_capture()
        pipe.skip_step("dedup", "Testing skip")
        assert "dedup" in pipe.skipped_steps

    def test_skip_step_emits_alert(self) -> None:
        pipe, captured = _pipeline_with_capture()
        pipe.skip_step("dedup", "Manual override for back-fill")
        assert len(captured) == 1
        assert captured[0] == ("dedup", "Manual override for back-fill")

    def test_multiple_steps_can_be_skipped(self) -> None:
        pipe, captured = _pipeline_with_capture()
        pipe.skip_step("dedup", "Reason A")
        pipe.skip_step("reconcile", "Reason B")
        assert len(pipe.skipped_steps) == 2
        assert len(captured) == 2

    @pytest.mark.parametrize("step_name", PIPELINE_STEP_NAMES)
    def test_any_valid_step_can_be_skipped(self, step_name: str) -> None:
        pipe, _ = _pipeline_with_capture()
        entry = pipe.skip_step(step_name, f"Test skip of {step_name}")
        assert entry.step_name == step_name

    def test_skipped_steps_accessor_returns_copy(self) -> None:
        """Mutating the returned dict must not affect internal state."""
        pipe, _ = _pipeline_with_capture()
        pipe.skip_step("dedup", "Reason")
        snapshot = pipe.skipped_steps
        snapshot["dedup"] = None  # type: ignore[assignment]
        # Internal state should be unaffected
        assert pipe.skipped_steps["dedup"] is not None

    def test_reason_is_stripped_of_leading_trailing_whitespace(self) -> None:
        pipe, captured = _pipeline_with_capture()
        entry = pipe.skip_step("gap_detect", "  trimmed reason  ")
        assert entry.override_reason == "trimmed reason"
        assert captured[0][1] == "trimmed reason"


# ---------------------------------------------------------------------------
# ValidationPipeline — skipped steps in execution
# ---------------------------------------------------------------------------


class TestSkippedStepsInExecution:
    def test_skipped_step_not_in_steps_completed(self) -> None:
        pipe, _ = _pipeline_with_capture()
        pipe.skip_step("dedup", "Test skip")
        result = pipe.run(_make_dataset())

        assert result.ok is True
        assert "dedup" not in result.steps_completed

    def test_skipped_step_dataset_passes_through(self) -> None:
        """Even with a step skipped, dataset should reach the end intact."""
        pipe, _ = _pipeline_with_capture()
        pipe.skip_step("reconcile", "No secondary provider in test")
        result = pipe.run(_make_dataset(ltp=777.0))

        assert result.ok is True
        assert result.dataset is not None
        assert result.dataset["ltp"] == 777.0

    def test_all_other_steps_run_when_one_is_skipped(self) -> None:
        pipe, _ = _pipeline_with_capture()
        pipe.skip_step("dedup", "skip for test")

        result = pipe.run(_make_dataset())

        # All steps except 'dedup' should be in steps_completed
        expected = [s for s in PIPELINE_STEP_NAMES if s != "dedup"]
        assert result.steps_completed == expected

    def test_multiple_skipped_steps_all_excluded_from_completed(self) -> None:
        pipe, _ = _pipeline_with_capture()
        pipe.skip_step("dedup", "Reason 1")
        pipe.skip_step("gap_detect", "Reason 2")
        pipe.skip_step("reconcile", "Reason 3")

        result = pipe.run(_make_dataset())

        skipped = {"dedup", "gap_detect", "reconcile"}
        expected = [s for s in PIPELINE_STEP_NAMES if s not in skipped]
        assert result.steps_completed == expected


# ---------------------------------------------------------------------------
# ValidationPipeline — PipelineResult field correctness
# ---------------------------------------------------------------------------


class TestPipelineResultFieldCorrectness:
    def test_success_result_has_all_fields(self) -> None:
        pipe = ValidationPipeline()
        result = pipe.run(_make_dataset())

        assert isinstance(result.ok, bool)
        assert isinstance(result.steps_completed, list)
        assert result.poor_quality is False
        assert result.step_failed is None
        assert result.incident is None
        assert result.dataset is not None

    def test_failure_result_has_all_fields(self) -> None:
        pipe = ValidationPipeline()
        incident = DataIncident.create(
            incident_type="TEST",
            instrument_id="NSE:X:EQ",
            provider="p",
        )

        def fail(dataset: dict) -> tuple[dict, bool, Optional[DataIncident]]:
            return dataset, False, incident

        pipe._step_normalise = fail  # type: ignore[method-assign]
        result = pipe.run(_make_dataset())

        assert result.ok is False
        assert result.step_failed == "normalise"
        assert result.incident is incident
        assert isinstance(result.steps_completed, list)
        assert result.poor_quality is False

    def test_steps_completed_is_a_list(self) -> None:
        pipe = ValidationPipeline()
        result = pipe.run(_make_dataset())
        assert isinstance(result.steps_completed, list)

    def test_steps_completed_length_matches_step_count(self) -> None:
        pipe = ValidationPipeline()
        result = pipe.run(_make_dataset())
        assert len(result.steps_completed) == len(PIPELINE_STEP_NAMES)

    def test_poor_quality_result_steps_completed_excludes_deliver(self) -> None:
        pipe = ValidationPipeline()

        def low_score(dataset: dict) -> tuple[dict, bool, Optional[DataIncident]]:
            dataset["_confidenceScore"] = 10
            return dataset, True, None

        pipe._step_quality_score = low_score  # type: ignore[method-assign]
        result = pipe.run(_make_dataset())

        assert "deliver" not in result.steps_completed
        # But persist should be there
        assert "persist" in result.steps_completed


# ---------------------------------------------------------------------------
# ValidationPipeline — PIPELINE_STEP_NAMES constant
# ---------------------------------------------------------------------------


class TestPipelineStepNamesConstant:
    def test_exactly_14_steps(self) -> None:
        assert len(PIPELINE_STEP_NAMES) == 14

    def test_all_expected_step_names_present(self) -> None:
        expected = {
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
        }
        assert set(PIPELINE_STEP_NAMES) == expected

    def test_step_names_are_in_logical_order(self) -> None:
        assert PIPELINE_STEP_NAMES[0] == "raw_receipt"
        assert PIPELINE_STEP_NAMES[1] == "schema_validate"
        assert PIPELINE_STEP_NAMES[9] == "quality_score"
        assert PIPELINE_STEP_NAMES[12] == "persist"
        assert PIPELINE_STEP_NAMES[13] == "deliver"
