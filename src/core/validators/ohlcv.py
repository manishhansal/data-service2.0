"""
OHLCV Candle Invariant Enforcement — Task 5.3.

This module validates that every OHLCV candle satisfies the fundamental
price-bar invariants before it is persisted to the database or served
to consumers.  It is called as part of Validation Pipeline step 2–3.

Invariants enforced (Requirements 4.3, 13.8):
  1. ``high >= max(open, close)``
  2. ``low  <= min(open, close)``
  3. ``volume >= 0``
  4. All price values (open, high, low, close) are strictly > 0

Indian market 3m block (Requirements 1.5, 4.2, 10.11):
  - ``interval="3m"`` raises ``ValueError`` BEFORE any validation logic runs.
  - This is a hard, unconditional block at the validator layer.
  - The block applies ONLY to Indian market data; Binance crypto 3m is
    handled by the crypto provider path, not this module.

Constants
---------
CANONICAL_INDIAN_TIMEFRAMES
    Tuple of all supported Indian-market candle intervals.  ``"3m"`` is
    permanently excluded.

Requirements: 4.3, 1.5, 13.8, 17.1
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any, Optional

from src.observability.logging import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# The canonical set of intervals supported for Indian market data.
# 3m is explicitly absent — it is permanently unsupported.
CANONICAL_INDIAN_TIMEFRAMES: tuple[str, ...] = (
    "1m",
    "5m",
    "10m",
    "15m",
    "30m",
    "1h",
    "1d",
    "1w",
    "1M",
)

# Human-readable names for each OHLCV invariant used in incident details.
_INVARIANT_LABELS: dict[str, str] = {
    "high_ge_max_open_close": "high >= max(open, close)",
    "low_le_min_open_close":  "low <= min(open, close)",
    "volume_non_negative":    "volume >= 0",
    "open_gt_zero":           "open > 0",
    "high_gt_zero":           "high > 0",
    "low_gt_zero":            "low > 0",
    "close_gt_zero":          "close > 0",
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_ohlcv_invariants(
    candle: dict,
    provider: str,
    instrument_id: str,
    interval: str,
    *,
    is_indian_market: bool = True,
) -> tuple[bool, Optional[dict]]:
    """Validate that a candle dict satisfies all OHLCV invariants.

    Parameters
    ----------
    candle        : Normalised candle dict with ``open``, ``high``, ``low``,
                    ``close``, ``volume`` keys.
    provider      : Provider identifier (included in DataIncident on failure).
    instrument_id : Canonical instrument identifier.
    interval      : Candle interval string (e.g. ``"1m"``, ``"1d"``).
    is_indian_market : When ``True`` (default), the 3m hard-block is enforced.
                       Set to ``False`` for Binance crypto candles.

    Returns
    -------
    ``(True, None)``               — all invariants satisfied.
    ``(False, incident_dict)``     — at least one invariant failed.

    Raises
    ------
    ValueError
        When ``is_indian_market=True`` and ``interval == "3m"``.  This is an
        unconditional hard block; no further validation runs.

    Requirements: 4.3, 1.5, 17.1
    """
    # ── Hard block: 3m is permanently unsupported for Indian market data ──
    if is_indian_market and interval == "3m":
        raise ValueError(
            "interval 3m is permanently unsupported for Indian market data"
        )

    # ── Extract values ────────────────────────────────────────────────────
    open_p  = _to_float(candle.get("open"))
    high_p  = _to_float(candle.get("high"))
    low_p   = _to_float(candle.get("low"))
    close_p = _to_float(candle.get("close"))
    volume  = _to_float(candle.get("volume"))

    # Collect all invariant violations before returning
    failed_invariants: list[str] = []
    rejected_values: dict[str, Any] = {}

    # ── Presence checks (cannot validate further if fields are missing) ───
    missing = [
        k for k, v in {
            "open": open_p, "high": high_p, "low": low_p, "close": close_p
        }.items()
        if v is None
    ]
    if missing:
        incident = _make_incident(
            instrument_id=instrument_id,
            interval=interval,
            timestamp=candle.get("time"),
            provider=provider,
            failed_invariants=[f"missing_field:{f}" for f in missing],
            rejected_values={f: candle.get(f) for f in missing},
        )
        logger.warning(
            "ohlcv_invariant_missing_price_fields",
            component="ohlcv_validator",
            instrument_id=instrument_id,
            interval=interval,
            missing_fields=missing,
            provider=provider,
        )
        return False, incident

    # ── All prices must be > 0 ────────────────────────────────────────────
    price_checks = {
        "open_gt_zero":  (open_p, open_p > 0),      # type: ignore[operator]
        "high_gt_zero":  (high_p, high_p > 0),      # type: ignore[operator]
        "low_gt_zero":   (low_p,  low_p > 0),       # type: ignore[operator]
        "close_gt_zero": (close_p, close_p > 0),    # type: ignore[operator]
    }
    for inv_key, (val, passes) in price_checks.items():
        if not passes:
            failed_invariants.append(inv_key)
            rejected_values[inv_key.replace("_gt_zero", "")] = val

    # ── high >= max(open, close) ──────────────────────────────────────────
    if high_p < max(open_p, close_p):               # type: ignore[operator]
        failed_invariants.append("high_ge_max_open_close")
        rejected_values["high"] = high_p
        rejected_values["max_open_close"] = max(open_p, close_p)

    # ── low <= min(open, close) ───────────────────────────────────────────
    if low_p > min(open_p, close_p):                # type: ignore[operator]
        failed_invariants.append("low_le_min_open_close")
        rejected_values["low"] = low_p
        rejected_values["min_open_close"] = min(open_p, close_p)

    # ── volume >= 0 ───────────────────────────────────────────────────────
    # volume may be None when volumeUnavailable=True; skip the check in that case
    if volume is not None and volume < 0:
        failed_invariants.append("volume_non_negative")
        rejected_values["volume"] = volume

    if failed_invariants:
        incident = _make_incident(
            instrument_id=instrument_id,
            interval=interval,
            timestamp=candle.get("time"),
            provider=provider,
            failed_invariants=failed_invariants,
            rejected_values=rejected_values,
        )
        logger.warning(
            "ohlcv_invariant_violation",
            component="ohlcv_validator",
            instrument_id=instrument_id,
            interval=interval,
            provider=provider,
            failed_invariants=failed_invariants,
            rejected_values=rejected_values,
        )
        return False, incident

    return True, None


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _to_float(value: Any) -> Optional[float]:
    """Convert a value to float, returning None on failure."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _make_incident(
    *,
    instrument_id: str,
    interval: str,
    timestamp: Any,
    provider: str,
    failed_invariants: list[str],
    rejected_values: dict[str, Any],
) -> dict:
    """Build a DataIncident-compatible dict for an OHLCV invariant failure.

    Returns a plain dict so this module has no circular dependency on
    pipeline.py.  The ValidationPipeline converts this to a proper
    DataIncident when it processes the result.

    Fields align with the design's DataIncident schema:
        instrumentId, intervalStr, timestamp, failedInvariant,
        rejectedValues, detectedAt.

    Requirements: 4.3
    """
    detected_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    return {
        "incidentId": str(uuid.uuid4()),
        "incidentType": "OHLC_INVARIANT",
        "instrumentId": instrument_id,
        "provider": provider,
        "timestamp": detected_at,
        "severity": "HIGH",
        "details": {
            "intervalStr": interval,
            "candleTimestamp": str(timestamp) if timestamp is not None else None,
            "failedInvariant": failed_invariants[0] if len(failed_invariants) == 1 else failed_invariants,
            "failedInvariants": failed_invariants,
            "failedInvariantLabels": [
                _INVARIANT_LABELS.get(inv, inv) for inv in failed_invariants
            ],
            "rejectedValues": rejected_values,
            "detectedAt": detected_at,
        },
    }
