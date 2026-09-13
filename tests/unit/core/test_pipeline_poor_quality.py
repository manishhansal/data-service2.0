"""
Unit tests for the POOR_QUALITY flag and pipeline delivery control.

Specifically covers:
- ValidationPipeline._apply_poor_quality_rule() helper method
- Score < 60 → dataset["_poorQuality"] = True, returns (dataset, True)
- Score == 60 → returns (dataset, False) — threshold is exclusive
- Score > 60 → returns (dataset, False)
- When pipeline runs and quality_score sets _confidenceScore < 60:
    - step 13 (persist) still runs
    - step 14 (deliver) does NOT run
    - result.poor_quality is True
    - result.ok is True (poor quality is not a pipeline failure)
    - "_poorQuality" key is present in result.dataset
- The _apply_poor_quality_rule method is wired into the run() method
  via the quality_score step

Requirements: 17.7
"""

from __future__ import annotations

from typing import Optional

import pytest

from src.core.pipeline import (
    POOR_QUALITY_SCORE_THRESHOLD,
    DataIncident,
    ValidationPipeline,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_dataset(**kwargs) -> dict:
    defaults = {
        "instrumentId": "NSE:NIFTY:IDX",
        "provider": "angel_one",
        "ltp": 22000.0,
    }
    defaults.update(kwargs)
    return defaults


def _pipeline_with_score(score: int) -> ValidationPipeline:
    """Return a pipeline whose quality_score step injects ``score``."""
    pipe = ValidationPipeline()

    def quality_step(dataset: dict) -> tuple[dict, bool, Optional[DataIncident]]:
        dataset["_confidenceScore"] = score
        return dataset, True, None

    pipe._step_quality_score = quality_step  # type: ignore[method-assign]
    return pipe


# ---------------------------------------------------------------------------
# _apply_poor_quality_rule() — direct unit tests
# ---------------------------------------------------------------------------


class TestApplyPoorQualityRule:
    def setup_method(self) -> None:
        self.pipe = ValidationPipeline()

    def test_score_59_sets_poor_quality_flag(self) -> None:
        dataset = _make_dataset()
        result_ds, is_poor = self.pipe._apply_poor_quality_rule(dataset, 59)
        assert is_poor is True
        assert result_ds["_poorQuality"] is True

    def test_score_0_sets_poor_quality_flag(self) -> None:
        dataset = _make_dataset()
        result_ds, is_poor = self.pipe._apply_poor_quality_rule(dataset, 0)
        assert is_poor is True
        assert result_ds["_poorQuality"] is True

    def test_score_1_sets_poor_quality_flag(self) -> None:
        dataset = _make_dataset()
        result_ds, is_poor = self.pipe._apply_poor_quality_rule(dataset, 1)
        assert is_poor is True
        assert result_ds.get("_poorQuality") is True

    def test_score_60_does_not_set_poor_quality_flag(self) -> None:
        dataset = _make_dataset()
        result_ds, is_poor = self.pipe._apply_poor_quality_rule(
            dataset, POOR_QUALITY_SCORE_THRESHOLD
        )
        assert is_poor is False
        assert "_poorQuality" not in result_ds

    def test_score_61_does_not_set_poor_quality_flag(self) -> None:
        dataset = _make_dataset()
        result_ds, is_poor = self.pipe._apply_poor_quality_rule(dataset, 61)
        assert is_poor is False

    def test_score_95_does_not_set_poor_quality_flag(self) -> None:
        dataset = _make_dataset()
        result_ds, is_poor = self.pipe._apply_poor_quality_rule(dataset, 95)
        assert is_poor is False

    def test_returns_same_dataset_object(self) -> None:
        """The method must mutate and return the same dict (in-place)."""
        dataset = _make_dataset()
        original_id = id(dataset)
        result_ds, _ = self.pipe._apply_poor_quality_rule(dataset, 30)
        assert id(result_ds) == original_id

    @pytest.mark.parametrize("score", [0, 1, 29, 59])
    def test_various_poor_quality_scores(self, score: int) -> None:
        dataset = _make_dataset()
        _, is_poor = self.pipe._apply_poor_quality_rule(dataset, score)
        assert is_poor is True

    @pytest.mark.parametrize("score", [60, 61, 79, 80, 95])
    def test_various_non_poor_quality_scores(self, score: int) -> None:
        dataset = _make_dataset()
        _, is_poor = self.pipe._apply_poor_quality_rule(dataset, score)
        assert is_poor is False

    def test_threshold_boundary_is_59_poor_60_not(self) -> None:
        ds_poor = _make_dataset()
        ds_ok = _make_dataset()
        _, poor_59 = self.pipe._apply_poor_quality_rule(ds_poor, 59)
        _, poor_60 = self.pipe._apply_poor_quality_rule(ds_ok, 60)
        assert poor_59 is True
        assert poor_60 is False

    def test_poor_quality_flag_not_set_when_score_is_threshold(self) -> None:
        dataset = _make_dataset()
        result_ds, is_poor = self.pipe._apply_poor_quality_rule(dataset, 60)
        assert is_poor is False
        # The _poorQuality key should NOT be present at all
        assert "_poorQuality" not in result_ds


# ---------------------------------------------------------------------------
# Pipeline integration — _apply_poor_quality_rule wired into run()
# ---------------------------------------------------------------------------


class TestPoorQualityPipelineIntegration:
    def test_score_below_threshold_sets_poor_quality_in_result(self) -> None:
        pipe = _pipeline_with_score(45)
        result = pipe.run(_make_dataset())
        assert result.poor_quality is True

    def test_score_at_threshold_does_not_set_poor_quality(self) -> None:
        pipe = _pipeline_with_score(POOR_QUALITY_SCORE_THRESHOLD)
        result = pipe.run(_make_dataset())
        assert result.poor_quality is False

    def test_score_above_threshold_does_not_set_poor_quality(self) -> None:
        pipe = _pipeline_with_score(80)
        result = pipe.run(_make_dataset())
        assert result.poor_quality is False

    def test_poor_quality_dataset_has_flag_in_dict(self) -> None:
        pipe = _pipeline_with_score(30)
        result = pipe.run(_make_dataset())
        assert result.dataset is not None
        assert result.dataset.get("_poorQuality") is True

    def test_poor_quality_result_ok_is_true(self) -> None:
        """poor_quality is a flag, NOT a pipeline failure — ok must be True."""
        pipe = _pipeline_with_score(10)
        result = pipe.run(_make_dataset())
        assert result.ok is True
        assert result.step_failed is None
        assert result.incident is None

    def test_persist_runs_for_poor_quality_dataset(self) -> None:
        """Step 13 (persist) must execute even when poor_quality=True."""
        steps_called: list[str] = []

        pipe = _pipeline_with_score(25)

        def tracking_persist(dataset: dict) -> tuple[dict, bool, Optional[DataIncident]]:
            steps_called.append("persist")
            return dataset, True, None

        pipe._step_persist = tracking_persist  # type: ignore[method-assign]
        result = pipe.run(_make_dataset())

        assert result.poor_quality is True
        assert "persist" in steps_called, "persist MUST run for poor_quality datasets"

    def test_deliver_does_not_run_for_poor_quality_dataset(self) -> None:
        """Step 14 (deliver) must be suppressed when poor_quality=True."""
        steps_called: list[str] = []

        pipe = _pipeline_with_score(25)

        def tracking_deliver(dataset: dict) -> tuple[dict, bool, Optional[DataIncident]]:
            steps_called.append("deliver")
            return dataset, True, None

        pipe._step_deliver = tracking_deliver  # type: ignore[method-assign]
        result = pipe.run(_make_dataset())

        assert result.poor_quality is True
        assert "deliver" not in steps_called, "deliver must NOT run for poor_quality datasets"

    def test_deliver_not_in_steps_completed_for_poor_quality(self) -> None:
        pipe = _pipeline_with_score(5)
        result = pipe.run(_make_dataset())
        assert "deliver" not in result.steps_completed

    def test_persist_in_steps_completed_for_poor_quality(self) -> None:
        pipe = _pipeline_with_score(5)
        result = pipe.run(_make_dataset())
        assert "persist" in result.steps_completed

    def test_normal_pipeline_deliver_runs(self) -> None:
        """When score >= 60, deliver must run (i.e., not suppressed)."""
        steps_called: list[str] = []

        pipe = _pipeline_with_score(80)

        def tracking_deliver(dataset: dict) -> tuple[dict, bool, Optional[DataIncident]]:
            steps_called.append("deliver")
            return dataset, True, None

        pipe._step_deliver = tracking_deliver  # type: ignore[method-assign]
        result = pipe.run(_make_dataset())

        assert result.poor_quality is False
        assert "deliver" in steps_called

    def test_confidence_score_preserved_in_dataset(self) -> None:
        """The _confidenceScore injected by quality_score step is accessible
        in the final dataset."""
        pipe = _pipeline_with_score(45)
        result = pipe.run(_make_dataset())
        assert result.dataset is not None
        assert result.dataset.get("_confidenceScore") == 45

    @pytest.mark.parametrize("score,expect_poor", [
        (0, True),
        (29, True),
        (59, True),
        (60, False),
        (61, False),
        (95, False),
    ])
    def test_parametrized_score_boundary(self, score: int, expect_poor: bool) -> None:
        pipe = _pipeline_with_score(score)
        result = pipe.run(_make_dataset())
        assert result.poor_quality is expect_poor
