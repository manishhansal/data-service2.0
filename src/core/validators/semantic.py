"""
Semantic validation — pipeline step 5.

Enforces field-level integrity rules that go beyond JSON schema compliance.
Every rule here prevents the platform from fabricating or misrepresenting
data that the upstream provider did not actually supply.

Non-negotiable rules (Requirements 6.2–6.6, 3.3)
--------------------------------------------------
``oi``
    Must NEVER be populated from ``tradedValue``.  If ``oi`` equals
    ``tradedValue`` and was not independently provided by the provider,
    set ``oi = None`` and ``oi_missing = True``.

``tradedValue``
    Represents total traded value in INR — completely distinct from ``oi``
    (open interest in contracts).  The two fields must never be swapped.

``iv``
    ``null`` when not provided by the source.  Zero IV is NOT a substitute
    for a missing IV value.

``delta``, ``gamma``, ``theta``, ``vega``, ``rho`` (Greeks)
    ``null`` when not provided.  Placeholder zeros are prohibited.

``bid``, ``ask``
    ``null`` when not actually provided.  Placeholder zeros are prohibited.

Convention for "explicitly provided"
--------------------------------------
Each check uses a companion ``_provided`` boolean field that the Normaliser
(task 5.2) is responsible for setting.  For example:

    ``oi_explicitly_provided``  → True when the raw response contained ``oi``
    ``iv_explicitly_provided``  → True when the raw response contained ``iv``

If these helper fields are absent, the validator applies conservative logic:
if the suspicious-zero-or-match condition is met, the field is nulled out.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any, Optional

__all__ = [
    "validate_oi_not_from_traded_value",
    "validate_iv_not_zero_substituted",
    "validate_greeks_not_zero_substituted",
    "validate_bid_ask_not_zero_substituted",
    "run_semantic_validation",
]

# ---------------------------------------------------------------------------
# Minimal DataIncident placeholder (full schema in schemas/incident.py)
# ---------------------------------------------------------------------------


def _make_incident(
    *,
    incident_type: str,
    instrument_id: str,
    provider: str,
    severity: str = "HIGH",
    details: Optional[dict] = None,
) -> dict:
    """Return a lightweight DataIncident dict for pipeline consumption."""
    return {
        "incidentId": str(uuid.uuid4()),
        "incidentType": incident_type,
        "instrumentId": instrument_id,
        "provider": provider,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "severity": severity,
        "details": details or {},
    }


# ---------------------------------------------------------------------------
# Individual semantic checks
# ---------------------------------------------------------------------------


def validate_oi_not_from_traded_value(record: dict) -> tuple[bool, str]:
    """Ensure ``oi`` is not populated from ``tradedValue``.

    Logic
    -----
    - If ``oi`` equals ``tradedValue`` (numeric equality) **and** the
      ``oi_explicitly_provided`` flag is absent or ``False`` → OI was not
      independently supplied by the provider; null it out.
    - Mutates ``record`` in place: sets ``oi = None``, ``oi_missing = True``.
    - Returns ``(False, reason)`` when the substitution was detected and
      corrected; ``(True, "")`` otherwise.

    Note: returning ``False`` here signals a semantic correction was made,
    not a fatal pipeline failure.  The caller (``run_semantic_validation``)
    decides whether to generate a DataIncident.

    Requirements: 6.2, 3.3
    """
    oi = record.get("oi")
    traded_value = record.get("tradedValue")
    explicitly_provided: bool = record.get("oi_explicitly_provided", False)

    if oi is None:
        # Nothing to substitute — mark missing flag if not already set.
        if not record.get("oiMissing"):
            record["oiMissing"] = True
        return True, ""

    if explicitly_provided:
        # Provider genuinely supplied this value.
        return True, ""

    # Numeric equality check (handle float imprecision conservatively)
    if traded_value is not None and _numeric_equal(oi, traded_value):
        record["oi"] = None
        record["oiMissing"] = True
        reason = (
            f"oi ({oi!r}) equals tradedValue ({traded_value!r}) and was not "
            "explicitly provided by the provider — set to null (oi_missing=True)"
        )
        return False, reason

    return True, ""


def validate_iv_not_zero_substituted(record: dict) -> tuple[bool, str]:
    """Ensure ``iv`` is not a zero placeholder for a missing value.

    Logic
    -----
    - If ``iv == 0.0`` and ``iv_explicitly_provided`` is absent or ``False``
      → provider did not supply IV; null it out.
    - Returns ``(False, reason)`` when a zero-substitution was corrected.

    Requirements: 6.4
    """
    iv = record.get("iv")
    if iv is None:
        if not record.get("ivMissing"):
            record["ivMissing"] = True
        return True, ""

    explicitly_provided: bool = record.get("iv_explicitly_provided", False)
    if not explicitly_provided and _is_zero(iv):
        record["iv"] = None
        record["ivMissing"] = True
        reason = (
            "iv is 0.0 but was not explicitly provided by the provider — "
            "zero IV is not a substitute for a missing IV; set to null (iv_missing=True)"
        )
        return False, reason

    return True, ""


def validate_greeks_not_zero_substituted(record: dict) -> tuple[bool, str]:
    """Ensure option Greeks are not zero placeholders for missing values.

    Greek fields checked: ``delta``, ``gamma``, ``theta``, ``vega``, ``rho``.
    Each is nulled out if ``== 0.0`` and not explicitly provided.

    Requirements: 6.5
    """
    greek_fields = ("delta", "gamma", "theta", "vega", "rho")
    corrections: list[str] = []

    for greek in greek_fields:
        value = record.get(greek)
        if value is None:
            continue  # already absent — OK

        provided_key = f"{greek}_explicitly_provided"
        explicitly_provided: bool = record.get(provided_key, False)

        if not explicitly_provided and _is_zero(value):
            record[greek] = None
            corrections.append(greek)

    if corrections:
        reason = (
            f"Greeks {corrections} were 0.0 without explicit provider supply — "
            "placeholder zeros are prohibited; set to null"
        )
        return False, reason

    return True, ""


def validate_bid_ask_not_zero_substituted(record: dict) -> tuple[bool, str]:
    """Ensure ``bid`` and ``ask`` are not zero placeholders.

    Requirements: 6.6
    """
    corrections: list[str] = []

    for field_name in ("bid", "ask"):
        value = record.get(field_name)
        if value is None:
            continue

        provided_key = f"{field_name}_explicitly_provided"
        explicitly_provided: bool = record.get(provided_key, False)

        if not explicitly_provided and _is_zero(value):
            record[field_name] = None
            corrections.append(field_name)

    if corrections:
        reason = (
            f"Fields {corrections} were 0.0 without explicit provider supply — "
            "placeholder zeros are prohibited; set to null"
        )
        return False, reason

    return True, ""


# ---------------------------------------------------------------------------
# Composite runner for the pipeline
# ---------------------------------------------------------------------------


def run_semantic_validation(
    record: dict,
    provider: str,
) -> tuple[dict, bool, Any]:
    """Run all four semantic validation checks on a record.

    This is the function wired into pipeline step 5
    (``ValidationPipeline._step_semantic_validate``).

    Parameters
    ----------
    record:
        The normalised dataset dict (mutated in place for corrections).
    provider:
        The originating provider identifier (for incident records).

    Returns
    -------
    ``(modified_record, ok, incident)``
    - ``ok`` is ``True`` when no *fatal* semantic violation was found (field
      corrections do not constitute fatal failures — the pipeline continues).
    - ``incident`` is ``None`` when ``ok`` is ``True``.
    - When ``ok`` is ``False`` (currently not triggered — all violations are
      corrective, not fatal), ``incident`` carries a DataIncident dict.
    """
    instrument_id: str = record.get("instrumentId", "UNKNOWN")
    corrections: list[str] = []

    # Run each check; collect correction reasons.
    checks = [
        ("OI_TRADED_VALUE_SUBSTITUTION",   validate_oi_not_from_traded_value),
        ("IV_ZERO_SUBSTITUTION",           validate_iv_not_zero_substituted),
        ("GREEKS_ZERO_SUBSTITUTION",       validate_greeks_not_zero_substituted),
        ("BID_ASK_ZERO_SUBSTITUTION",      validate_bid_ask_not_zero_substituted),
    ]

    for incident_type, check_fn in checks:
        ok, reason = check_fn(record)
        if not ok:
            corrections.append(f"{incident_type}: {reason}")

    if corrections:
        # Log all corrections as a single DataIncident (non-fatal — pipeline
        # continues; valid fields have already been corrected in-place).
        incident = _make_incident(
            incident_type="SEMANTIC_INTEGRITY",
            instrument_id=instrument_id,
            provider=provider,
            severity="MEDIUM",
            details={
                "corrections": corrections,
                "correctionCount": len(corrections),
            },
        )
        # Still return ok=True: corrections were applied; the record can
        # continue.  The incident is attached for audit/monitoring purposes.
        record["_semanticIncident"] = incident

    return record, True, None


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _is_zero(value: Any) -> bool:
    """Return True when *value* is numerically zero (int or float)."""
    if isinstance(value, (int, float)):
        return value == 0.0
    return False


def _numeric_equal(a: Any, b: Any) -> bool:
    """Return True when two numeric values are equal.

    Uses exact equality for integers and a tiny relative tolerance for floats
    to guard against floating-point representation noise.
    """
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        fa, fb = float(a), float(b)
        if fa == fb:
            return True
        # Relative tolerance: 1e-9
        max_abs = max(abs(fa), abs(fb))
        if max_abs == 0:
            return True
        return abs(fa - fb) / max_abs < 1e-9
    return False
