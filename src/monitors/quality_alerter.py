"""
src/monitors/quality_alerter.py

Quality Alert System for DATA-SERVICE 2.0.

Fires and manages structured alerts when the ``DataConfidenceScore`` or
quality grade for an active instrument degrades below configured thresholds
(Requirements 18.6, 7.7, 7.8).

Alert Rules
-----------
+--------------------------+----------+---------------------------------------+
| Condition                | Severity | Notes                                 |
+==========================+==========+=======================================+
| grade == "BLOCKED"       | CRITICAL | Always fire — score < 30              |
| grade == "LOW"           | WARNING  | score in [30, 50)                     |
| grade == "MEDIUM"        | WARNING  | score in [50, 80) AND score < 60      |
| grade == "HIGH"          | —        | No alert                              |
| grade == "MEDIUM"        | —        | score >= 60 — acceptable range        |
+--------------------------+----------+---------------------------------------+

Deduplication
-------------
Within any rolling 5-minute window, only ONE active alert per
``(symbol, severity)`` pair may exist.  Calling ``check_and_alert`` for the
same symbol+severity while a matching unresolved alert is younger than 5
minutes returns an empty list — no duplicate firing.

The in-process alert log stores all historical alerts (bounded to
``max_history`` entries; oldest are evicted on overflow in insertion order).

Public API
----------
QualityAlert            — Pydantic v2 model for a single alert record
QualityAlerter          — main alerter class

    check_and_alert(symbol, score, grade, reasons) -> list[QualityAlert]
        Evaluate alert rules; return newly fired alerts (empty list when no
        rule matches or dedup suppresses the fire).

    get_active_alerts() -> list[QualityAlert]
        Return all alerts that have not been resolved.

    resolve_alert(alert_id) -> bool
        Mark an alert as resolved; return True if found, False otherwise.

    get_alert_history(limit=100) -> list[QualityAlert]
        Return the most recent *limit* alerts (both resolved and active),
        ordered newest-first.

Requirements: 7.7, 7.8, 18.6
"""

from __future__ import annotations

import threading
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Final

from pydantic import BaseModel, Field

from src.observability.logging import get_logger

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Duration (seconds) within which a duplicate ``(symbol, severity)`` alert
#: is suppressed if an unresolved alert already exists.
_DEDUP_WINDOW_SEC: Final[int] = 300  # 5 minutes

#: Default maximum number of alert records retained in the history log.
_DEFAULT_MAX_HISTORY: Final[int] = 1_000

#: Grade thresholds imported from quality_engine constants to avoid coupling.
_GRADE_MEDIUM_SCORE_THRESHOLD: Final[int] = 60  # MEDIUM → WARNING when score < 60

#: Severity labels
_SEVERITY_CRITICAL: Final[str] = "CRITICAL"
_SEVERITY_WARNING: Final[str] = "WARNING"
_SEVERITY_INFO: Final[str] = "INFO"

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


class QualityAlert(BaseModel):
    """Immutable record of a single quality alert.

    Attributes:
        alertId:    Unique UUID v4 identifier assigned at creation.
        symbol:     Canonical instrument symbol (e.g. ``"NSE:NIFTY:IDX"``).
        severity:   One of ``"CRITICAL"``, ``"WARNING"``, ``"INFO"``.
        grade:      DataConfidenceScore grade string: ``"BLOCKED"``,
                    ``"LOW"``, ``"MEDIUM"``, ``"HIGH"``.
        score:      DataConfidenceScore integer in [0, 95].
        reasons:    List of human-readable block-reason strings from the
                    Quality Engine.
        firedAt:    UTC ISO-8601 timestamp (with ``Z`` suffix) when the
                    alert was fired.
        resolvedAt: UTC ISO-8601 timestamp when the alert was resolved, or
                    ``None`` if still active.
        resolved:   ``True`` once ``resolve_alert`` has been called.
    """

    alertId: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="UUID v4 alert identifier",
    )
    symbol: str = Field(..., min_length=1, description="Canonical instrument symbol")
    severity: str = Field(
        ...,
        pattern=r"^(CRITICAL|WARNING|INFO)$",
        description="Alert severity level",
    )
    grade: str = Field(
        ...,
        pattern=r"^(BLOCKED|LOW|MEDIUM|HIGH)$",
        description="DataConfidenceScore grade",
    )
    score: int = Field(..., ge=0, le=95, description="DataConfidenceScore")
    reasons: list[str] = Field(
        default_factory=list,
        description="Block/degradation reasons from Quality Engine",
    )
    firedAt: str = Field(..., description="UTC ISO-8601 timestamp of alert creation")
    resolvedAt: str | None = Field(
        default=None,
        description="UTC ISO-8601 timestamp of resolution, or null if unresolved",
    )
    resolved: bool = Field(
        default=False,
        description="True when the alert has been resolved",
    )


# ---------------------------------------------------------------------------
# Alert rule helpers
# ---------------------------------------------------------------------------


def _determine_severity(grade: str, score: int) -> str | None:
    """Map a (grade, score) pair to a severity level, or None if no alert.

    Rules (in priority order):
      1. grade == "BLOCKED"                    → CRITICAL
      2. grade == "LOW"                        → WARNING
      3. grade == "MEDIUM" and score < 60      → WARNING
      4. grade == "HIGH"                       → None  (no alert)
      5. grade == "MEDIUM" and score >= 60     → None  (acceptable)

    Args:
        grade: DataConfidenceScore grade string.
        score: Integer DataConfidenceScore in [0, 95].

    Returns:
        Severity string or ``None`` when no alert should be fired.
    """
    if grade == "BLOCKED":
        return _SEVERITY_CRITICAL
    if grade == "LOW":
        return _SEVERITY_WARNING
    if grade == "MEDIUM" and score < _GRADE_MEDIUM_SCORE_THRESHOLD:
        return _SEVERITY_WARNING
    # grade == "HIGH", or MEDIUM with score >= 60 → no alert
    return None


def _utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string with ``Z`` suffix."""
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# ---------------------------------------------------------------------------
# QualityAlerter
# ---------------------------------------------------------------------------


class QualityAlerter:
    """In-process quality alert manager.

    Maintains a bounded, ordered history of :class:`QualityAlert` records and
    implements deduplication, resolution, and history retrieval.

    Thread-safe: all mutations are guarded by a ``threading.Lock``.

    Args:
        max_history:      Maximum number of alert records retained.  When the
                          limit is reached the oldest record is evicted before
                          a new one is inserted.  Default: 1,000.
        dedup_window_sec: Seconds within which a duplicate
                          ``(symbol, severity)`` alert is suppressed when an
                          unresolved alert already exists.  Default: 300 (5 min).
    """

    def __init__(
        self,
        max_history: int = _DEFAULT_MAX_HISTORY,
        dedup_window_sec: int = _DEDUP_WINDOW_SEC,
    ) -> None:
        if max_history < 1:
            raise ValueError(f"max_history must be >= 1, got {max_history}")
        if dedup_window_sec < 0:
            raise ValueError(
                f"dedup_window_sec must be >= 0, got {dedup_window_sec}"
            )

        self._max_history = max_history
        self._dedup_window_sec = dedup_window_sec

        # OrderedDict preserves insertion order for history eviction.
        # Key: alertId → Value: QualityAlert
        self._alerts: OrderedDict[str, QualityAlert] = OrderedDict()

        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def check_and_alert(
        self,
        symbol: str,
        score: int,
        grade: str,
        reasons: list[str],
    ) -> list[QualityAlert]:
        """Evaluate alert rules and fire new alerts when conditions are met.

        Applies deduplication: if an unresolved alert for the same
        ``(symbol, severity)`` already exists and was fired within
        ``dedup_window_sec`` seconds, no new alert is generated.

        Emits a ``quality_degraded`` or ``quality_restored`` structured log
        entry consistent with Requirements 18.6 / 7.7.

        Args:
            symbol:  Canonical instrument symbol.
            score:   DataConfidenceScore integer in [0, 95].
            grade:   Grade string returned by ``QualityEngine.grade_score``.
            reasons: List of block/degradation reason strings.

        Returns:
            A list of newly created :class:`QualityAlert` instances.  Empty
            when no rule fires or dedup suppresses firing.
        """
        severity = _determine_severity(grade, score)
        if severity is None:
            return []

        now = datetime.now(tz=timezone.utc)
        fired_at_str = now.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

        with self._lock:
            # Deduplication check: search for an active (unresolved) alert
            # matching (symbol, severity) that was fired within the window.
            if self._is_duplicate(symbol, severity, now):
                logger.debug(
                    "quality_alert_suppressed_dedup",
                    component="quality_alerter",
                    symbol=symbol,
                    severity=severity,
                    score=score,
                    grade=grade,
                )
                return []

            # Create and store the new alert.
            alert = QualityAlert(
                symbol=symbol,
                severity=severity,
                grade=grade,
                score=score,
                reasons=list(reasons),
                firedAt=fired_at_str,
            )
            self._store_alert(alert)

        # Structured log (Requirement 18.6)
        logger.warning(
            "quality_degraded",
            component="quality_alerter",
            alertId=alert.alertId,
            symbol=symbol,
            severity=severity,
            score=score,
            grade=grade,
            previousScore=None,  # caller may enrich if needed
            blockReasons=reasons,
        )

        return [alert]

    def get_active_alerts(self) -> list[QualityAlert]:
        """Return all unresolved alerts, ordered newest-first.

        Returns:
            List of :class:`QualityAlert` instances where ``resolved`` is
            ``False``.
        """
        with self._lock:
            active = [a for a in self._alerts.values() if not a.resolved]
        # Newest-first (reverse insertion order)
        active.sort(key=lambda a: a.firedAt, reverse=True)
        return active

    def resolve_alert(self, alert_id: str) -> bool:
        """Mark an alert as resolved.

        Updates ``resolved`` to ``True`` and sets ``resolvedAt`` to the
        current UTC time.  Emits a ``quality_restored`` structured log entry
        (Requirement 18.6).

        Args:
            alert_id: UUID string of the alert to resolve.

        Returns:
            ``True`` if the alert was found and resolved, ``False`` if no
            alert with that ID exists (or it was already resolved — resolving
            an already-resolved alert is idempotent and returns ``True``).
        """
        with self._lock:
            alert = self._alerts.get(alert_id)
            if alert is None:
                return False

            if alert.resolved:
                # Idempotent: already resolved
                return True

            resolved_at = (
                datetime.now(tz=timezone.utc)
                .strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
                + "Z"
            )
            # Pydantic v2 models are immutable by default; use model_copy.
            updated = alert.model_copy(
                update={"resolved": True, "resolvedAt": resolved_at}
            )
            self._alerts[alert_id] = updated
            resolved_alert = updated

        logger.info(
            "quality_restored",
            component="quality_alerter",
            alertId=alert_id,
            symbol=resolved_alert.symbol,
            score=resolved_alert.score,
            previousScore=None,
        )
        return True

    def get_alert_history(self, limit: int = 100) -> list[QualityAlert]:
        """Return the most recent *limit* alerts (active + resolved), newest-first.

        Args:
            limit: Maximum number of records to return.  Clamped to the
                   ``max_history`` value configured at construction.

        Returns:
            List of :class:`QualityAlert` instances, newest-first.

        Raises:
            ValueError: If *limit* is less than 1.
        """
        if limit < 1:
            raise ValueError(f"limit must be >= 1, got {limit}")
        effective_limit = min(limit, self._max_history)

        with self._lock:
            # OrderedDict preserves insertion order — reverse for newest-first.
            all_alerts = list(self._alerts.values())

        all_alerts.sort(key=lambda a: a.firedAt, reverse=True)
        return all_alerts[:effective_limit]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _is_duplicate(
        self,
        symbol: str,
        severity: str,
        now: datetime,
    ) -> bool:
        """Return True if an active (symbol, severity) alert exists within
        the dedup window.

        Must be called with ``self._lock`` held.

        Args:
            symbol:   Instrument symbol.
            severity: Alert severity to match.
            now:      Current UTC datetime.

        Returns:
            ``True`` when a duplicate should be suppressed.
        """
        cutoff_iso = (
            datetime(
                now.year,
                now.month,
                now.day,
                now.hour,
                now.minute,
                now.second,
                now.microsecond,
                tzinfo=timezone.utc,
            ).timestamp()
            - self._dedup_window_sec
        )
        for existing in self._alerts.values():
            if existing.resolved:
                continue
            if existing.symbol != symbol or existing.severity != severity:
                continue
            # Parse firedAt back to epoch seconds for comparison.
            try:
                fired_epoch = datetime.fromisoformat(
                    existing.firedAt.replace("Z", "+00:00")
                ).timestamp()
            except ValueError:
                continue
            if fired_epoch >= cutoff_iso:
                return True
        return False

    def _store_alert(self, alert: QualityAlert) -> None:
        """Insert *alert* into the ordered history, evicting oldest if full.

        Must be called with ``self._lock`` held.

        Args:
            alert: Alert record to store.
        """
        if len(self._alerts) >= self._max_history:
            # Evict the oldest entry (first key in insertion order).
            oldest_key = next(iter(self._alerts))
            del self._alerts[oldest_key]
        self._alerts[alert.alertId] = alert
