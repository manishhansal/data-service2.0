"""
src/engines/reconciliation_engine.py

Provider Reconciliation Engine for DATA-SERVICE 2.0.
this engine compares their observations, classifies the agreement level,
and produces a canonical observation based on documented routing rules.

Reconciliation classifications:
  MATCH               — providers agree within tolerance (LTP diff < 0.1%, OI diff < 1%)
  MINOR_DIFFERENCE    — small difference (LTP 0.1–0.5%, OI 1–5%)
  SIGNIFICANT_DIFFERENCE — notable difference (LTP > 0.5%, OI > 5%)
  STALE               — one provider's timestamp significantly older (> 5s delta)
  MISSING             — data only from one provider
  CONFLICT            — irreconcilable disagreement (OI direction opposite, etc.)

Canonical observation rules (deterministic, based on capability routing):
  LTP:
    - If providers agree (MATCH/MINOR): use the fresher provider's value
    - If SIGNIFICANT_DIFFERENCE: use Upstox (higher accuracy for FULL quote)
    - If one is STALE: use the fresher provider
  OI:
    - For F&O: prefer Angel One (dedicated OI endpoint, higher reliability)
    - If Angel One OI missing: use Upstox
  Greeks/IV:
    - Primary: Upstox V3 (more recent API, richer data)
    - Fallback: Angel One
  Depth:
    - For depth-5: either provider
    - For depth-30: Upstox only (full_d30 mode, Plus plan)

Design contracts:
  - Neither provider's observation is discarded — both stored in ReconciliationRecord.
  - canonical_observation is clearly tagged with which provider won and which rule.
  - Zero is NEVER substituted for missing OI.
  - Reconciliation records are durable (written to reconciliation_record table).

Requirements: Phase 12, Phase 34
"""

from __future__ import annotations

from typing import Any, Optional

from src.observability.logging import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Classification thresholds
# ---------------------------------------------------------------------------

_LTP_MATCH_PCT    = 0.001   # < 0.1% = MATCH
_LTP_MINOR_PCT    = 0.005   # 0.1–0.5% = MINOR
# > 0.5% = SIGNIFICANT

_OI_MATCH_PCT     = 0.01    # < 1% = MATCH
_OI_MINOR_PCT     = 0.05    # 1–5% = MINOR
# > 5% = SIGNIFICANT

_STALE_THRESHOLD_SEC = 5.0  # timestamp delta > 5s = STALE

# ---------------------------------------------------------------------------
# Reconciliation result
# ---------------------------------------------------------------------------


class ReconciliationResult:
    """Result of reconciling two provider observations for one instrument.

    Attributes:
        classification:    One of MATCH / MINOR_DIFFERENCE / SIGNIFICANT_DIFFERENCE
                           / STALE / MISSING / CONFLICT.
        canonical_value:   The authoritative value selected for downstream use.
        canonical_provider: Which provider's value was selected ("angel_one" / "upstox").
        resolution_rule:   Human-readable rule that determined the canonical value.
        provider_a_obs:    Provider A's raw observation dict.
        provider_b_obs:    Provider B's raw observation dict.
        ltp_diff_abs:      Absolute LTP difference.
        ltp_diff_pct:      Relative LTP difference.
        oi_diff_abs:       Absolute OI difference.
        iv_diff_abs:       Absolute IV difference.
        timestamp_diff_ms: Timestamp difference in milliseconds.
    """

    def __init__(
        self,
        classification: str,
        canonical_provider: Optional[str],
        canonical_ltp: Optional[float],
        canonical_oi: Optional[int],
        resolution_rule: str,
        provider_a: str,
        provider_a_obs: dict[str, Any],
        provider_b: str,
        provider_b_obs: dict[str, Any],
        ltp_diff_abs: Optional[float] = None,
        ltp_diff_pct: Optional[float] = None,
        oi_diff_abs: Optional[int] = None,
        iv_diff_abs: Optional[float] = None,
        timestamp_diff_ms: Optional[int] = None,
    ) -> None:
        self.classification = classification
        self.canonical_provider = canonical_provider
        self.canonical_ltp = canonical_ltp
        self.canonical_oi = canonical_oi
        self.resolution_rule = resolution_rule
        self.provider_a = provider_a
        self.provider_a_obs = provider_a_obs
        self.provider_b = provider_b
        self.provider_b_obs = provider_b_obs
        self.ltp_diff_abs = ltp_diff_abs
        self.ltp_diff_pct = ltp_diff_pct
        self.oi_diff_abs = oi_diff_abs
        self.iv_diff_abs = iv_diff_abs
        self.timestamp_diff_ms = timestamp_diff_ms

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a dict compatible with ReconciliationRecord ORM model."""
        return {
            "classification":   self.classification,
            "canonical_provider": self.canonical_provider,
            "canonical_ltp":    self.canonical_ltp,
            "canonical_oi":     self.canonical_oi,
            "resolution_rule":  self.resolution_rule,
            "provider_a":       self.provider_a,
            "provider_a_ltp":   self.provider_a_obs.get("ltp"),
            "provider_a_oi":    self.provider_a_obs.get("oi"),
            "provider_a_volume": self.provider_a_obs.get("volume"),
            "provider_a_bid":   self.provider_a_obs.get("bid"),
            "provider_a_ask":   self.provider_a_obs.get("ask"),
            "provider_a_iv":    self.provider_a_obs.get("iv"),
            "provider_a_timestamp": self.provider_a_obs.get("sourceTimestamp"),
            "provider_b":       self.provider_b,
            "provider_b_ltp":   self.provider_b_obs.get("ltp"),
            "provider_b_oi":    self.provider_b_obs.get("oi"),
            "provider_b_volume": self.provider_b_obs.get("volume"),
            "provider_b_bid":   self.provider_b_obs.get("bid"),
            "provider_b_ask":   self.provider_b_obs.get("ask"),
            "provider_b_iv":    self.provider_b_obs.get("iv"),
            "provider_b_timestamp": self.provider_b_obs.get("sourceTimestamp"),
            "ltp_diff_abs":     self.ltp_diff_abs,
            "ltp_diff_pct":     self.ltp_diff_pct,
            "oi_diff_abs":      self.oi_diff_abs,
            "iv_diff_abs":      self.iv_diff_abs,
            "timestamp_diff_ms": self.timestamp_diff_ms,
        }


# ---------------------------------------------------------------------------
# ReconciliationEngine
# ---------------------------------------------------------------------------


class ReconciliationEngine:
    """Compares provider observations and produces a canonical value.

    Stateless — safe for concurrent use.

    Usage:
        engine = ReconciliationEngine()

        # Reconcile two quotes
        result = engine.reconcile_quote(
            angel_obs=angel_normalized_quote,
            upstox_obs=upstox_normalized_quote,
            instrument_id="NSE_FO|NIFTY25OCTFUT",
        )

        # Use canonical values
        canonical_ltp = result.canonical_ltp
        canonical_oi  = result.canonical_oi
    """

    # ------------------------------------------------------------------
    # Quote reconciliation
    # ------------------------------------------------------------------

    def reconcile_quote(
        self,
        angel_obs: Optional[dict[str, Any]],
        upstox_obs: Optional[dict[str, Any]],
        instrument_id: str,
    ) -> ReconciliationResult:
        """Reconcile live quote observations from Angel One and Upstox.

        Args:
            angel_obs:     Normalized Angel One quote (or None if unavailable).
            upstox_obs:    Normalized Upstox quote (or None if unavailable).
            instrument_id: Canonical instrument ID.

        Returns:
            ReconciliationResult with classification and canonical values.
        """
        # --- Missing data case ---
        if angel_obs is None and upstox_obs is None:
            return ReconciliationResult(
                classification="MISSING",
                canonical_provider=None,
                canonical_ltp=None,
                canonical_oi=None,
                resolution_rule="both_providers_unavailable",
                provider_a="angel_one",
                provider_a_obs={},
                provider_b="upstox",
                provider_b_obs={},
            )

        if angel_obs is None:
            return ReconciliationResult(
                classification="MISSING",
                canonical_provider="upstox",
                canonical_ltp=upstox_obs.get("ltp"),  # type: ignore[union-attr]
                canonical_oi=upstox_obs.get("oi"),  # type: ignore[union-attr]
                resolution_rule="angel_one_unavailable_upstox_only",
                provider_a="angel_one",
                provider_a_obs={},
                provider_b="upstox",
                provider_b_obs=upstox_obs,  # type: ignore[arg-type]
            )

        if upstox_obs is None:
            return ReconciliationResult(
                classification="MISSING",
                canonical_provider="angel_one",
                canonical_ltp=angel_obs.get("ltp"),
                canonical_oi=angel_obs.get("oi"),
                resolution_rule="upstox_unavailable_angel_one_only",
                provider_a="angel_one",
                provider_a_obs=angel_obs,
                provider_b="upstox",
                provider_b_obs={},
            )

        # --- Both available: compare ---
        angel_ltp  = angel_obs.get("ltp")
        upstox_ltp = upstox_obs.get("ltp")
        angel_oi   = angel_obs.get("oi")
        upstox_oi  = upstox_obs.get("oi")
        angel_iv   = angel_obs.get("iv")
        upstox_iv  = upstox_obs.get("iv")

        # LTP difference
        ltp_diff_abs: Optional[float] = None
        ltp_diff_pct: Optional[float] = None
        if angel_ltp is not None and upstox_ltp is not None:
            ltp_diff_abs = abs(angel_ltp - upstox_ltp)
            if angel_ltp != 0:
                ltp_diff_pct = ltp_diff_abs / angel_ltp

        # OI difference
        oi_diff_abs: Optional[int] = None
        if angel_oi is not None and upstox_oi is not None:
            oi_diff_abs = abs(angel_oi - upstox_oi)

        # IV difference
        iv_diff_abs: Optional[float] = None
        if angel_iv is not None and upstox_iv is not None:
            iv_diff_abs = abs(angel_iv - upstox_iv)

        # Timestamp difference
        ts_diff_ms = _timestamp_diff_ms(
            angel_obs.get("sourceTimestamp"),
            upstox_obs.get("sourceTimestamp"),
        )

        # --- Classification ---
        classification = _classify_ltp_diff(ltp_diff_pct, ts_diff_ms)

        # --- Canonical value selection ---
        canonical_provider, canonical_ltp, canonical_oi, resolution_rule = (
            self._select_canonical_quote(
                angel_obs, upstox_obs,
                classification, ltp_diff_pct, ts_diff_ms,
            )
        )

        logger.debug(
            "reconciliation_result",
            component="reconciliation_engine",
            instrument_id=instrument_id,
            classification=classification,
            canonical_provider=canonical_provider,
            ltp_diff_pct=round(ltp_diff_pct * 100, 4) if ltp_diff_pct else None,
        )

        return ReconciliationResult(
            classification=classification,
            canonical_provider=canonical_provider,
            canonical_ltp=canonical_ltp,
            canonical_oi=canonical_oi,
            resolution_rule=resolution_rule,
            provider_a="angel_one",
            provider_a_obs=angel_obs,
            provider_b="upstox",
            provider_b_obs=upstox_obs,
            ltp_diff_abs=ltp_diff_abs,
            ltp_diff_pct=ltp_diff_pct,
            oi_diff_abs=oi_diff_abs,
            iv_diff_abs=iv_diff_abs,
            timestamp_diff_ms=ts_diff_ms,
        )

    def _select_canonical_quote(
        self,
        angel_obs: dict[str, Any],
        upstox_obs: dict[str, Any],
        classification: str,
        ltp_diff_pct: Optional[float],
        ts_diff_ms: Optional[int],
    ) -> tuple[str, Optional[float], Optional[int], str]:
        """Select the canonical LTP, OI, and explain the selection rule.

        Returns:
            (canonical_provider, canonical_ltp, canonical_oi, resolution_rule)
        """
        angel_ltp  = angel_obs.get("ltp")
        upstox_ltp = upstox_obs.get("ltp")
        angel_oi   = angel_obs.get("oi")
        upstox_oi  = upstox_obs.get("oi")

        # LTP selection
        if classification == "STALE":
            # Choose the fresher provider
            if _is_fresher(upstox_obs, angel_obs):
                canonical_ltp = upstox_ltp
                ltp_provider = "upstox"
                ltp_rule = "upstox_fresher_timestamp"
            else:
                canonical_ltp = angel_ltp
                ltp_provider = "angel_one"
                ltp_rule = "angel_one_fresher_timestamp"
        elif classification in ("MATCH", "MINOR_DIFFERENCE"):
            # Prefer the fresher provider
            if _is_fresher(upstox_obs, angel_obs):
                canonical_ltp = upstox_ltp
                ltp_provider = "upstox"
                ltp_rule = "providers_agree_upstox_fresher"
            else:
                canonical_ltp = angel_ltp
                ltp_provider = "angel_one"
                ltp_rule = "providers_agree_angel_one_fresher"
        elif classification == "SIGNIFICANT_DIFFERENCE":
            # Upstox FULL quote has richer price data
            canonical_ltp = upstox_ltp
            ltp_provider = "upstox"
            ltp_rule = "significant_diff_upstox_full_quote_preferred"
        else:
            # CONFLICT or unknown: prefer Upstox
            canonical_ltp = upstox_ltp
            ltp_provider = "upstox"
            ltp_rule = "conflict_upstox_default"

        # OI selection: Angel One preferred (dedicated OI endpoint)
        # OI MUST NOT be substituted with zero
        if angel_oi is not None:
            canonical_oi = angel_oi
            oi_rule = "angel_one_oi_preferred"
        elif upstox_oi is not None:
            canonical_oi = upstox_oi
            oi_rule = "upstox_oi_fallback"
        else:
            canonical_oi = None
            oi_rule = "oi_unavailable_both_providers"

        # Overall canonical provider = whichever won the LTP decision
        resolution_rule = f"ltp:{ltp_rule}|oi:{oi_rule}"

        return ltp_provider, canonical_ltp, canonical_oi, resolution_rule

    # ------------------------------------------------------------------
    # Greeks reconciliation
    # ------------------------------------------------------------------

    def reconcile_greeks(
        self,
        angel_greeks: Optional[dict[str, Any]],
        upstox_greeks: Optional[dict[str, Any]],
        instrument_id: str,
    ) -> dict[str, Any]:
        """Select canonical Greeks from two provider observations.

        Rule: Upstox V3 Greeks preferred (more recent API, higher precision).
        Angel One Greeks used as fallback when Upstox is unavailable.

        Neither set of provider Greeks is lost — both are stored in the
        canonical output under providerGreeks.angelOne and providerGreeks.upstox.

        Args:
            angel_greeks:  Normalized Angel One Greeks dict (or None).
            upstox_greeks: Normalized Upstox Greeks dict (or None).
            instrument_id: Canonical instrument ID.

        Returns:
            Canonical Greeks dict with both provider observations preserved.
        """
        result: dict[str, Any] = {
            "instrumentId": instrument_id,
            "iv":           None,
            "delta":        None,
            "gamma":        None,
            "theta":        None,
            "vega":         None,
            "rho":          None,
            "providerGreeks": {
                "angelOne": angel_greeks or {},
                "upstox":   upstox_greeks or {},
            },
            "greekSource": "NONE",
        }

        # Upstox first (preferred)
        if upstox_greeks and not upstox_greeks.get("greeksMissing", True):
            result["iv"]     = upstox_greeks.get("iv")
            result["delta"]  = upstox_greeks.get("delta")
            result["gamma"]  = upstox_greeks.get("gamma")
            result["theta"]  = upstox_greeks.get("theta")
            result["vega"]   = upstox_greeks.get("vega")
            result["rho"]    = upstox_greeks.get("rho")
            result["greekSource"] = "UPSTOX_V3"
        elif angel_greeks and not angel_greeks.get("greeksMissing", True):
            result["iv"]     = angel_greeks.get("iv")
            result["delta"]  = angel_greeks.get("delta")
            result["gamma"]  = angel_greeks.get("gamma")
            result["theta"]  = angel_greeks.get("theta")
            result["vega"]   = angel_greeks.get("vega")
            result["rho"]    = angel_greeks.get("rho")  # None from Angel One
            result["greekSource"] = "ANGEL_ONE"

        return result

    # ------------------------------------------------------------------
    # OI reconciliation (NEW — Phase I)
    # ------------------------------------------------------------------

    def reconcile_oi(
        self,
        *,
        provider_a: str,
        oi_a: Optional[int],
        oi_a_status: str,        # "LIVE", "HISTORICAL", "BLOCKED_BY_PROVIDER_PLAN", "UNAVAILABLE"
        provider_b: str,
        oi_b: Optional[int],
        oi_b_status: str,
        instrument_id: str,
        interval: str,
        candle_time_ms: Optional[int] = None,
    ) -> dict[str, Any]:
        """Reconcile open interest from two providers.

        OI handling rules (non-negotiable):
        - NULL OI from a provider means the provider did NOT supply OI.
          It MUST NOT be treated as zero.
        - Zero OI is valid only when the provider explicitly returns 0.
        - BLOCKED_BY_PROVIDER_PLAN must be surfaced; never substituted with 0.
        - Cross-provider comparison requires aligned candle timestamps.

        Args:
            provider_a:      First provider name (e.g. "angel_one").
            oi_a:            OI value from provider A. None = not supplied.
            oi_a_status:     Why provider A's OI has this value.
            provider_b:      Second provider name (e.g. "upstox").
            oi_b:            OI value from provider B. None = not supplied.
            oi_b_status:     Why provider B's OI has this value.
            instrument_id:   Canonical instrument ID.
            interval:        Candle interval string.
            candle_time_ms:  Candle open timestamp (UTC epoch ms) for alignment.

        Returns:
            OI reconciliation dict with keys:
                canonical_oi, canonical_provider, oi_status,
                oi_provider_a, oi_provider_b, oi_deviation_pct,
                oi_a_status, oi_b_status, notes
        """
        result: dict[str, Any] = {
            "instrument_id":   instrument_id,
            "interval":        interval,
            "candle_time_ms":  candle_time_ms,
            "provider_a":      provider_a,
            "oi_provider_a":   oi_a,
            "oi_a_status":     oi_a_status,
            "provider_b":      provider_b,
            "oi_provider_b":   oi_b,
            "oi_b_status":     oi_b_status,
            "canonical_oi":    None,
            "canonical_provider": None,
            "oi_deviation_pct": None,
            "oi_status":       "UNKNOWN",
            "notes":           [],
        }

        # Both blocked/unavailable
        if oi_a is None and oi_b is None:
            notes = []
            if "BLOCKED" in oi_a_status.upper():
                notes.append(f"{provider_a}: {oi_a_status}")
            if "BLOCKED" in oi_b_status.upper():
                notes.append(f"{provider_b}: {oi_b_status}")
            result["oi_status"] = "NULL_UNAVAILABLE"
            result["notes"] = notes
            logger.debug(
                "oi_reconciliation_both_null",
                component="reconciliation_engine",
                instrument_id=instrument_id,
                oi_a_status=oi_a_status,
                oi_b_status=oi_b_status,
            )
            return result

        # Only one provider has OI
        if oi_a is None:
            result["canonical_oi"] = oi_b
            result["canonical_provider"] = provider_b
            result["oi_status"] = "SINGLE_PROVIDER"
            result["notes"] = [f"{provider_a} OI unavailable: {oi_a_status}"]
            return result

        if oi_b is None:
            result["canonical_oi"] = oi_a
            result["canonical_provider"] = provider_a
            result["oi_status"] = "SINGLE_PROVIDER"
            result["notes"] = [f"{provider_b} OI unavailable: {oi_b_status}"]
            return result

        # Both have OI — compare
        if oi_a == 0 and oi_b == 0:
            result["canonical_oi"] = 0
            result["canonical_provider"] = provider_a
            result["oi_deviation_pct"] = 0.0
            result["oi_status"] = "CONFIRMED"
            return result

        # Calculate percentage deviation
        denom = max(abs(oi_a), abs(oi_b))
        if denom == 0:
            oi_dev_pct = 0.0
        else:
            oi_dev_pct = round(abs(oi_a - oi_b) / denom * 100.0, 4)

        result["oi_deviation_pct"] = oi_dev_pct

        if oi_dev_pct <= 0.5:
            result["oi_status"] = "CONFIRMED"
        elif oi_dev_pct <= 2.0:
            result["oi_status"] = "MINOR_DISCREPANCY"
        else:
            result["oi_status"] = "MAJOR_DISCREPANCY"
            result["notes"] = [
                f"OI deviation {oi_dev_pct:.2f}% exceeds 2% threshold. "
                f"{provider_a}={oi_a} vs {provider_b}={oi_b}"
            ]

        # Canonical selection: prefer Angel One (dedicated OI endpoint)
        # unless Angel One was BLOCKED
        if "BLOCKED" not in oi_a_status.upper():
            result["canonical_oi"] = oi_a
            result["canonical_provider"] = provider_a
        else:
            result["canonical_oi"] = oi_b
            result["canonical_provider"] = provider_b

        logger.debug(
            "oi_reconciliation_result",
            component="reconciliation_engine",
            instrument_id=instrument_id,
            oi_status=result["oi_status"],
            oi_dev_pct=oi_dev_pct,
        )

        return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _classify_ltp_diff(
    ltp_diff_pct: Optional[float],
    ts_diff_ms: Optional[int],
) -> str:
    """Classify the agreement level based on LTP difference and timestamp delta."""
    if ts_diff_ms is not None and abs(ts_diff_ms) > _STALE_THRESHOLD_SEC * 1000:
        return "STALE"

    if ltp_diff_pct is None:
        return "MISSING"

    if ltp_diff_pct < _LTP_MATCH_PCT:
        return "MATCH"
    elif ltp_diff_pct < _LTP_MINOR_PCT:
        return "MINOR_DIFFERENCE"
    else:
        return "SIGNIFICANT_DIFFERENCE"


def _timestamp_diff_ms(
    ts_a: Optional[str],
    ts_b: Optional[str],
) -> Optional[int]:
    """Compute the absolute timestamp difference in milliseconds."""
    if ts_a is None or ts_b is None:
        return None
    try:
        import datetime as _dt
        a = _dt.datetime.fromisoformat(ts_a)
        b = _dt.datetime.fromisoformat(ts_b)
        if a.tzinfo is None:
            a = a.replace(tzinfo=_dt.timezone.utc)
        if b.tzinfo is None:
            b = b.replace(tzinfo=_dt.timezone.utc)
        return int(abs((a - b).total_seconds() * 1000))
    except (ValueError, TypeError):
        return None


def _is_fresher(
    obs_a: dict[str, Any],
    obs_b: dict[str, Any],
) -> bool:
    """Return True if obs_a has a more recent source timestamp than obs_b."""
    ts_a_raw = obs_a.get("sourceTimestamp") or obs_a.get("receivedAt")
    ts_b_raw = obs_b.get("sourceTimestamp") or obs_b.get("receivedAt")
    if ts_a_raw is None:
        return False
    if ts_b_raw is None:
        return True
    try:
        import datetime as _dt
        a = _dt.datetime.fromisoformat(ts_a_raw)
        b = _dt.datetime.fromisoformat(ts_b_raw)
        return a > b
    except (ValueError, TypeError):
        return False
