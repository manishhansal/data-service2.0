"""
tests/unit/monitors/test_quality_alerter.py

Unit tests for QualityAlerter and QualityAlert.

Coverage:
- Alert rule mapping: grade × score → severity (CRITICAL / WARNING / None)
- No alert for grade HIGH regardless of score
- No alert for grade MEDIUM when score >= 60
- BLOCKED always fires CRITICAL
- LOW always fires WARNING
- MEDIUM + score < 60 fires WARNING
- Deduplication: same (symbol, severity) within 5-minute window → suppressed
- Deduplication: different symbol or different severity → not suppressed
- Resolved alerts do not block new alerts for the same symbol+severity
- resolve_alert: marks alert resolved, sets resolvedAt, returns True
- resolve_alert on unknown ID returns False
- resolve_alert is idempotent (second call returns True, no state change)
- get_active_alerts: returns only unresolved alerts
- get_alert_history: returns newest-first, respects limit
- get_alert_history: limit < 1 raises ValueError
- History eviction: oldest alert dropped when max_history reached
- QualityAlert model: UUID v4 alertId, firedAt UTC Z suffix, resolved default False
- Multiple different symbols each fire their own alerts
- Multiple rapid calls for the same symbol+grade accumulate if severity changes

Requirements: 7.7, 7.8, 18.6
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from typing import Any

import pytest

from src.monitors.quality_alerter import (
    QualityAlert,
    QualityAlerter,
    _determine_severity,
)


# ---------------------------------------------------------------------------
# _determine_severity — alert rule unit tests
# ---------------------------------------------------------------------------


class TestDetermineSeverity:
    """Tests for the pure _determine_severity rule function."""

    def test_blocked_always_critical(self) -> None:
        assert _determine_severity("BLOCKED", 0) == "CRITICAL"
        assert _determine_severity("BLOCKED", 29) == "CRITICAL"

    def test_low_always_warning(self) -> None:
        assert _determine_severity("LOW", 30) == "WARNING"
        assert _determine_severity("LOW", 49) == "WARNING"

    def test_medium_below_60_is_warning(self) -> None:
        assert _determine_severity("MEDIUM", 50) == "WARNING"
        assert _determine_severity("MEDIUM", 59) == "WARNING"

    def test_medium_at_or_above_60_is_none(self) -> None:
        assert _determine_severity("MEDIUM", 60) is None
        assert _determine_severity("MEDIUM", 79) is None

    def test_high_is_none(self) -> None:
        assert _determine_severity("HIGH", 80) is None
        assert _determine_severity("HIGH", 95) is None


# ---------------------------------------------------------------------------
# QualityAlert model
# ---------------------------------------------------------------------------


class TestQualityAlertModel:
    """Tests for the QualityAlert Pydantic v2 model itself."""

    def test_default_resolved_is_false(self) -> None:
        alert = QualityAlert(
            symbol="NSE:NIFTY:IDX",
            severity="CRITICAL",
            grade="BLOCKED",
            score=10,
            reasons=["score < 30"],
            firedAt="2024-01-01T09:00:00.000Z",
        )
        assert alert.resolved is False
        assert alert.resolvedAt is None

    def test_alert_id_is_valid_uuid4(self) -> None:
        alert = QualityAlert(
            symbol="NSE:BANKNIFTY:IDX",
            severity="WARNING",
            grade="LOW",
            score=35,
            reasons=[],
            firedAt="2024-01-01T09:00:00.000Z",
        )
        parsed = uuid.UUID(alert.alertId)
        assert parsed.version == 4

    def test_fired_at_z_suffix(self) -> None:
        """firedAt from check_and_alert must end with Z."""
        alerter = QualityAlerter()
        alerts = alerter.check_and_alert("SYM", 10, "BLOCKED", ["reason"])
        assert alerts[0].firedAt.endswith("Z")

    def test_score_bounds_validation(self) -> None:
        with pytest.raises(Exception):
            QualityAlert(
                symbol="X",
                severity="CRITICAL",
                grade="BLOCKED",
                score=96,  # out of range
                reasons=[],
                firedAt="2024-01-01T00:00:00.000Z",
            )

    def test_severity_pattern_validation(self) -> None:
        with pytest.raises(Exception):
            QualityAlert(
                symbol="X",
                severity="DANGER",  # invalid
                grade="BLOCKED",
                score=10,
                reasons=[],
                firedAt="2024-01-01T00:00:00.000Z",
            )

    def test_grade_pattern_validation(self) -> None:
        with pytest.raises(Exception):
            QualityAlert(
                symbol="X",
                severity="CRITICAL",
                grade="POOR",  # invalid
                score=10,
                reasons=[],
                firedAt="2024-01-01T00:00:00.000Z",
            )


# ---------------------------------------------------------------------------
# QualityAlerter.check_and_alert — firing rules
# ---------------------------------------------------------------------------


class TestCheckAndAlertFiringRules:
    """Tests that check_and_alert fires or suppresses based on grade/score."""

    def test_blocked_fires_critical(self) -> None:
        alerter = QualityAlerter()
        alerts = alerter.check_and_alert("SYM", 10, "BLOCKED", ["blocked reason"])
        assert len(alerts) == 1
        assert alerts[0].severity == "CRITICAL"
        assert alerts[0].grade == "BLOCKED"
        assert alerts[0].score == 10

    def test_low_fires_warning(self) -> None:
        alerter = QualityAlerter()
        alerts = alerter.check_and_alert("SYM", 40, "LOW", ["low reason"])
        assert len(alerts) == 1
        assert alerts[0].severity == "WARNING"

    def test_medium_below_60_fires_warning(self) -> None:
        alerter = QualityAlerter()
        alerts = alerter.check_and_alert("SYM", 55, "MEDIUM", ["degraded"])
        assert len(alerts) == 1
        assert alerts[0].severity == "WARNING"

    def test_medium_at_60_no_alert(self) -> None:
        alerter = QualityAlerter()
        alerts = alerter.check_and_alert("SYM", 60, "MEDIUM", [])
        assert alerts == []

    def test_medium_above_60_no_alert(self) -> None:
        alerter = QualityAlerter()
        alerts = alerter.check_and_alert("SYM", 70, "MEDIUM", [])
        assert alerts == []

    def test_high_no_alert(self) -> None:
        alerter = QualityAlerter()
        alerts = alerter.check_and_alert("SYM", 85, "HIGH", [])
        assert alerts == []

    def test_reasons_preserved_in_alert(self) -> None:
        alerter = QualityAlerter()
        reasons = ["score < 30", "provider unhealthy"]
        alerts = alerter.check_and_alert("SYM", 15, "BLOCKED", reasons)
        assert alerts[0].reasons == reasons

    def test_symbol_preserved_in_alert(self) -> None:
        alerter = QualityAlerter()
        alerts = alerter.check_and_alert("NSE:RELIANCE:EQ", 10, "BLOCKED", [])
        assert alerts[0].symbol == "NSE:RELIANCE:EQ"


# ---------------------------------------------------------------------------
# QualityAlerter.check_and_alert — deduplication
# ---------------------------------------------------------------------------


class TestDeduplication:
    """Tests for the 5-minute dedup window."""

    def test_duplicate_same_symbol_severity_suppressed(self) -> None:
        alerter = QualityAlerter()
        first = alerter.check_and_alert("SYM", 10, "BLOCKED", ["r1"])
        second = alerter.check_and_alert("SYM", 5, "BLOCKED", ["r2"])
        assert len(first) == 1
        assert len(second) == 0

    def test_different_symbol_not_suppressed(self) -> None:
        alerter = QualityAlerter()
        first = alerter.check_and_alert("SYM_A", 10, "BLOCKED", [])
        second = alerter.check_and_alert("SYM_B", 10, "BLOCKED", [])
        assert len(first) == 1
        assert len(second) == 1

    def test_different_severity_not_suppressed(self) -> None:
        """LOW then BLOCKED for the same symbol → both fire (different severity)."""
        alerter = QualityAlerter()
        first = alerter.check_and_alert("SYM", 40, "LOW", [])
        second = alerter.check_and_alert("SYM", 10, "BLOCKED", [])
        assert len(first) == 1
        assert len(second) == 1

    def test_resolved_alert_allows_new_alert(self) -> None:
        """After resolving an alert, the same (symbol, severity) can fire again."""
        alerter = QualityAlerter()
        first = alerter.check_and_alert("SYM", 10, "BLOCKED", [])
        assert len(first) == 1

        alerter.resolve_alert(first[0].alertId)

        second = alerter.check_and_alert("SYM", 5, "BLOCKED", ["new reason"])
        assert len(second) == 1

    def test_dedup_window_zero_disables_dedup(self) -> None:
        """dedup_window_sec=0 means every call fires a new alert."""
        alerter = QualityAlerter(dedup_window_sec=0)
        first = alerter.check_and_alert("SYM", 10, "BLOCKED", [])
        second = alerter.check_and_alert("SYM", 10, "BLOCKED", [])
        assert len(first) == 1
        assert len(second) == 1


# ---------------------------------------------------------------------------
# QualityAlerter.resolve_alert
# ---------------------------------------------------------------------------


class TestResolveAlert:
    """Tests for resolve_alert behaviour."""

    def test_resolve_known_alert_returns_true(self) -> None:
        alerter = QualityAlerter()
        alerts = alerter.check_and_alert("SYM", 10, "BLOCKED", [])
        alert_id = alerts[0].alertId
        result = alerter.resolve_alert(alert_id)
        assert result is True

    def test_resolve_sets_resolved_true(self) -> None:
        alerter = QualityAlerter()
        alerts = alerter.check_and_alert("SYM", 10, "BLOCKED", [])
        alert_id = alerts[0].alertId
        alerter.resolve_alert(alert_id)
        history = alerter.get_alert_history()
        resolved = next(a for a in history if a.alertId == alert_id)
        assert resolved.resolved is True

    def test_resolve_sets_resolved_at(self) -> None:
        alerter = QualityAlerter()
        alerts = alerter.check_and_alert("SYM", 10, "BLOCKED", [])
        alert_id = alerts[0].alertId
        alerter.resolve_alert(alert_id)
        history = alerter.get_alert_history()
        resolved = next(a for a in history if a.alertId == alert_id)
        assert resolved.resolvedAt is not None
        assert resolved.resolvedAt.endswith("Z")

    def test_resolve_unknown_id_returns_false(self) -> None:
        alerter = QualityAlerter()
        result = alerter.resolve_alert("00000000-0000-0000-0000-000000000000")
        assert result is False

    def test_resolve_idempotent(self) -> None:
        """Resolving the same alert twice returns True both times."""
        alerter = QualityAlerter()
        alerts = alerter.check_and_alert("SYM", 10, "BLOCKED", [])
        alert_id = alerts[0].alertId
        assert alerter.resolve_alert(alert_id) is True
        assert alerter.resolve_alert(alert_id) is True


# ---------------------------------------------------------------------------
# QualityAlerter.get_active_alerts
# ---------------------------------------------------------------------------


class TestGetActiveAlerts:
    """Tests for get_active_alerts."""

    def test_no_alerts_returns_empty(self) -> None:
        alerter = QualityAlerter()
        assert alerter.get_active_alerts() == []

    def test_unresolved_alert_is_active(self) -> None:
        alerter = QualityAlerter()
        alerter.check_and_alert("SYM", 10, "BLOCKED", [])
        active = alerter.get_active_alerts()
        assert len(active) == 1

    def test_resolved_alert_not_in_active(self) -> None:
        alerter = QualityAlerter()
        alerts = alerter.check_and_alert("SYM", 10, "BLOCKED", [])
        alerter.resolve_alert(alerts[0].alertId)
        assert alerter.get_active_alerts() == []

    def test_multiple_alerts_only_active_returned(self) -> None:
        alerter = QualityAlerter()
        a1 = alerter.check_and_alert("SYM_A", 10, "BLOCKED", [])
        a2 = alerter.check_and_alert("SYM_B", 40, "LOW", [])
        alerter.resolve_alert(a1[0].alertId)

        active = alerter.get_active_alerts()
        assert len(active) == 1
        assert active[0].symbol == "SYM_B"


# ---------------------------------------------------------------------------
# QualityAlerter.get_alert_history
# ---------------------------------------------------------------------------


class TestGetAlertHistory:
    """Tests for get_alert_history."""

    def test_empty_history(self) -> None:
        alerter = QualityAlerter()
        assert alerter.get_alert_history() == []

    def test_history_contains_all_alerts(self) -> None:
        alerter = QualityAlerter()
        alerter.check_and_alert("SYM_A", 10, "BLOCKED", [])
        alerter.check_and_alert("SYM_B", 40, "LOW", [])
        history = alerter.get_alert_history()
        assert len(history) == 2

    def test_history_includes_resolved_alerts(self) -> None:
        alerter = QualityAlerter()
        alerts = alerter.check_and_alert("SYM", 10, "BLOCKED", [])
        alerter.resolve_alert(alerts[0].alertId)
        history = alerter.get_alert_history()
        assert len(history) == 1
        assert history[0].resolved is True

    def test_history_newest_first(self) -> None:
        """Alerts are returned ordered newest-first (by firedAt)."""
        alerter = QualityAlerter()
        alerter.check_and_alert("SYM_A", 10, "BLOCKED", [])
        # Small sleep to ensure distinct timestamps
        time.sleep(0.01)
        alerter.check_and_alert("SYM_B", 40, "LOW", [])
        history = alerter.get_alert_history()
        assert history[0].symbol == "SYM_B"
        assert history[1].symbol == "SYM_A"

    def test_history_limit_respected(self) -> None:
        alerter = QualityAlerter()
        for i in range(5):
            # Use dedup_window_sec=0 so each fires; different symbols avoid dedup
            alerter.check_and_alert(f"SYM_{i}", 10, "BLOCKED", [])
        history = alerter.get_alert_history(limit=3)
        assert len(history) == 3

    def test_history_limit_less_than_1_raises(self) -> None:
        alerter = QualityAlerter()
        with pytest.raises(ValueError, match="limit must be >= 1"):
            alerter.get_alert_history(limit=0)

    def test_history_limit_clamped_to_max_history(self) -> None:
        """limit > max_history is silently clamped — no error."""
        alerter = QualityAlerter(max_history=3)
        for i in range(3):
            alerter.check_and_alert(f"SYM_{i}", 10, "BLOCKED", [])
        # Requesting more than max_history should not raise; returns at most max_history
        history = alerter.get_alert_history(limit=1000)
        assert len(history) <= 3


# ---------------------------------------------------------------------------
# History eviction
# ---------------------------------------------------------------------------


class TestHistoryEviction:
    """Tests that max_history eviction works correctly."""

    def test_oldest_evicted_when_capacity_reached(self) -> None:
        alerter = QualityAlerter(max_history=3)
        symbols = ["SYM_A", "SYM_B", "SYM_C", "SYM_D"]
        for sym in symbols:
            alerter.check_and_alert(sym, 10, "BLOCKED", [])

        history = alerter.get_alert_history(limit=10)
        # Only 3 most recent should remain
        assert len(history) == 3
        present_symbols = {a.symbol for a in history}
        assert "SYM_A" not in present_symbols
        assert "SYM_D" in present_symbols

    def test_max_history_1_retains_only_latest(self) -> None:
        alerter = QualityAlerter(max_history=1)
        alerter.check_and_alert("SYM_A", 10, "BLOCKED", [])
        alerter.check_and_alert("SYM_B", 10, "BLOCKED", [])
        history = alerter.get_alert_history()
        assert len(history) == 1
        assert history[0].symbol == "SYM_B"


# ---------------------------------------------------------------------------
# Constructor validation
# ---------------------------------------------------------------------------


class TestConstructorValidation:
    """Tests for invalid constructor arguments."""

    def test_max_history_zero_raises(self) -> None:
        with pytest.raises(ValueError, match="max_history must be >= 1"):
            QualityAlerter(max_history=0)

    def test_dedup_window_negative_raises(self) -> None:
        with pytest.raises(ValueError, match="dedup_window_sec must be >= 0"):
            QualityAlerter(dedup_window_sec=-1)


# ---------------------------------------------------------------------------
# Multi-symbol independence
# ---------------------------------------------------------------------------


class TestMultiSymbol:
    """Tests that alerts for different symbols are independent."""

    def test_multiple_symbols_fire_independently(self) -> None:
        alerter = QualityAlerter()
        results: dict[str, list[Any]] = {}
        for sym in ["NIFTY", "BANKNIFTY", "RELIANCE"]:
            results[sym] = alerter.check_and_alert(sym, 10, "BLOCKED", [])
        assert all(len(v) == 1 for v in results.values())

    def test_resolving_one_does_not_affect_others(self) -> None:
        alerter = QualityAlerter()
        a = alerter.check_and_alert("SYM_A", 10, "BLOCKED", [])
        alerter.check_and_alert("SYM_B", 10, "BLOCKED", [])
        alerter.resolve_alert(a[0].alertId)

        active = alerter.get_active_alerts()
        assert len(active) == 1
        assert active[0].symbol == "SYM_B"
