"""
Normaliser — Task 5.2: Map provider-specific raw responses to canonical form
with strict null semantics.

The Normaliser is the single point where provider-specific field names, value
conventions, and missing-data representations are converted to the platform's
canonical schema.  It enforces all null rules defined in the design and
requirements as non-negotiable invariants:

  - ``oi``: ``None`` + ``oi_missing: True`` when provider does not supply it.
            **NEVER** populated from ``tradedValue``.
  - ``tradedValue``: semantically distinct from ``oi``; never interchangeable.
  - ``iv``: ``None`` when not provided; zero IV is NOT a substitute.
  - ``delta``, ``gamma``, ``theta``, ``vega``, ``rho``: ``None`` when not
            provided; placeholder zeros are prohibited.
  - ``bid``, ``ask``: ``None`` when not provided; placeholder zeros prohibited.
  - ``volume``: when source does not supply it: ``volume = 0`` +
            ``volume_unavailable: True``.

Partial-response handling: if a subset of fields is invalid, those fields are
set to ``None`` with the corresponding missing flag.  The rest of the
normalised output is returned (the entire response is NOT dropped).

Complete schema validation failure: structured error is logged and the
function returns ``(raw, False, incident)`` — the pipeline drops the dataset.

Every normalised dataset carries:
  - ``normalisationVersion``: semver string (default ``"2.0.0"``)
  - per-field ``MetricTag``: ``OBSERVED`` / ``DERIVED`` / ``MODELLED``
    (default ``OBSERVED`` when undetermined)

Three public entry points are provided for the three normalisation contexts:
  - ``normalise_ohlcv``         — historical / backfill candle bars
  - ``normalise_quote``         — live quote snapshots
  - ``normalise_option_chain_row`` — individual option chain contract row

Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7, 6.8, 6.9, 6.10, 6.11, 6.12
"""

from __future__ import annotations

import datetime
import hashlib
import uuid
from enum import Enum
from typing import Any, Optional

from src.observability.logging import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

NORMALISATION_VERSION: str = "2.0.0"

# Fields whose presence (even as zero / empty string) must be validated as
# genuinely provider-supplied before they are accepted.
_ZERO_PROHIBITED_FLOAT_FIELDS: frozenset[str] = frozenset(
    {"iv", "delta", "gamma", "theta", "vega", "rho", "bid", "ask"}
)


# ---------------------------------------------------------------------------
# MetricTag enum
# ---------------------------------------------------------------------------


class MetricTag(str, Enum):
    """Provenance class of a single analytics field.

    OBSERVED  — directly from exchange feed
    DERIVED   — computed from observed values (e.g. changePct, pcrOi)
    MODELLED  — from a pricing model (e.g. iv, delta from a model)
    """

    OBSERVED = "OBSERVED"
    DERIVED = "DERIVED"
    MODELLED = "MODELLED"


# ---------------------------------------------------------------------------
# Field tag maps — declare the MetricTag for every canonical field
# ---------------------------------------------------------------------------

_OHLCV_FIELD_TAGS: dict[str, MetricTag] = {
    "time": MetricTag.OBSERVED,
    "open": MetricTag.OBSERVED,
    "high": MetricTag.OBSERVED,
    "low": MetricTag.OBSERVED,
    "close": MetricTag.OBSERVED,
    "volume": MetricTag.OBSERVED,
    "oi": MetricTag.OBSERVED,
    "volumeUnavailable": MetricTag.OBSERVED,
    "oiMissing": MetricTag.OBSERVED,
    "provider": MetricTag.OBSERVED,
    "normalisationVersion": MetricTag.OBSERVED,
}

_QUOTE_FIELD_TAGS: dict[str, MetricTag] = {
    "instrumentId": MetricTag.OBSERVED,
    "symbol": MetricTag.OBSERVED,
    "exchange": MetricTag.OBSERVED,
    "ltp": MetricTag.OBSERVED,
    "open": MetricTag.OBSERVED,
    "high": MetricTag.OBSERVED,
    "low": MetricTag.OBSERVED,
    "prevClose": MetricTag.OBSERVED,
    "change": MetricTag.DERIVED,
    "changePct": MetricTag.DERIVED,
    "volume": MetricTag.OBSERVED,
    "oi": MetricTag.OBSERVED,
    "tradedValue": MetricTag.OBSERVED,
    "totalBuyQty": MetricTag.OBSERVED,
    "totalSellQty": MetricTag.OBSERVED,
    "upperCircuit": MetricTag.OBSERVED,
    "lowerCircuit": MetricTag.OBSERVED,
    "weekHigh52": MetricTag.OBSERVED,
    "weekLow52": MetricTag.OBSERVED,
    "lastTradeTime": MetricTag.OBSERVED,
    "bid": MetricTag.OBSERVED,
    "ask": MetricTag.OBSERVED,
    "marketStatus": MetricTag.OBSERVED,
    "oiMissing": MetricTag.OBSERVED,
    "bidAskMissing": MetricTag.OBSERVED,
    "volumeUnavailable": MetricTag.OBSERVED,
    "normalisationVersion": MetricTag.OBSERVED,
}

_OPTION_ROW_FIELD_TAGS: dict[str, MetricTag] = {
    "strike": MetricTag.OBSERVED,
    "optionType": MetricTag.OBSERVED,
    "ltp": MetricTag.OBSERVED,
    "bid": MetricTag.OBSERVED,
    "ask": MetricTag.OBSERVED,
    "oi": MetricTag.OBSERVED,
    "oiChange": MetricTag.OBSERVED,
    "volume": MetricTag.OBSERVED,
    "tradedValue": MetricTag.OBSERVED,
    "iv": MetricTag.MODELLED,
    "delta": MetricTag.MODELLED,
    "gamma": MetricTag.MODELLED,
    "theta": MetricTag.MODELLED,
    "vega": MetricTag.MODELLED,
    "rho": MetricTag.MODELLED,
    "oiMissing": MetricTag.OBSERVED,
    "oiChangeMissing": MetricTag.OBSERVED,
    "volumeMissing": MetricTag.OBSERVED,
    "ivMissing": MetricTag.OBSERVED,
    "greeksMissing": MetricTag.OBSERVED,
    "bidAskMissing": MetricTag.OBSERVED,
    "normalisationVersion": MetricTag.OBSERVED,
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _make_incident(
    incident_type: str,
    instrument_id: str,
    provider: str,
    severity: str,
    details: dict,
) -> dict:
    """Build a minimal DataIncident-compatible dict.

    Returns a plain dict so this module does not import from pipeline.py
    (avoiding circular imports).  The ValidationPipeline wraps this in a
    proper DataIncident when processing steps.
    """
    return {
        "incidentId": str(uuid.uuid4()),
        "incidentType": incident_type,
        "instrumentId": instrument_id,
        "provider": provider,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "severity": severity,
        "details": details,
    }


def _raw_response_hash(raw: dict) -> str:
    """Compute a short SHA-256 hex digest of the repr of a raw dict.

    Used in schema-failure incident details (Requirement 6.12).
    """
    content = repr(sorted(raw.items())).encode("utf-8", errors="replace")
    return hashlib.sha256(content).hexdigest()[:32]


def _is_genuinely_supplied(raw: dict, field: str) -> bool:
    """Return True only when ``field`` is present in ``raw`` and not None.

    A value of ``0`` or ``0.0`` is considered *supplied* (the provider sent it
    deliberately).  ``None`` and missing key are treated as *not supplied*.
    """
    return field in raw and raw[field] is not None


def _safe_float(value: Any, allow_zero: bool = True) -> Optional[float]:
    """Convert a value to float, returning None on failure.

    If ``allow_zero`` is False and the converted value is 0.0, returns None
    (used for fields where zero is not a valid substitute for a missing value).
    """
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not allow_zero and result == 0.0:
        return None
    return result


def _safe_int(value: Any) -> Optional[int]:
    """Convert a value to int, returning None on failure."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _apply_metric_tags(output: dict, tag_map: dict[str, MetricTag]) -> dict:
    """Attach ``_metricTags`` mapping to the output dict.

    Fields not found in ``tag_map`` default to ``OBSERVED``.
    Only tags for fields that exist in ``output`` are included.
    """
    tags: dict[str, str] = {}
    for field in output:
        if field.startswith("_"):
            continue
        tags[field] = tag_map.get(field, MetricTag.OBSERVED).value
    output["_metricTags"] = tags
    return output


# ---------------------------------------------------------------------------
# OI null-semantic enforcement
# ---------------------------------------------------------------------------


def _resolve_oi(
    raw: dict,
    *,
    oi_key: str = "oi",
    traded_value_key: str = "tradedValue",
) -> tuple[Optional[int], bool]:
    """Resolve ``oi`` from the raw dict, enforcing null semantics.

    Rules (non-negotiable, Requirements 6.2, 3.3):
    1. If the raw response does not supply ``oi_key`` → return (None, True)
    2. If ``raw[oi_key]`` is None → return (None, True)
    3. CRITICAL: if the ``oi`` value equals the ``tradedValue`` value, that is
       a substitution violation — reject it and return (None, True).
    4. Otherwise convert to int and return (value, False).

    Returns (oi_value, oi_missing).
    """
    traded_value = raw.get(traded_value_key)

    if oi_key not in raw or raw[oi_key] is None:
        return None, True

    raw_oi = raw[oi_key]

    # Detect the OI = tradedValue substitution (semantic integrity violation)
    if traded_value is not None and raw_oi == traded_value:
        logger.warning(
            "normaliser_oi_traded_value_substitution_rejected",
            component="normaliser",
            oi_key=oi_key,
            traded_value_key=traded_value_key,
            value=raw_oi,
        )
        return None, True

    converted = _safe_int(raw_oi)
    if converted is None:
        return None, True

    return converted, False


# ---------------------------------------------------------------------------
# Greeks null-semantic enforcement
# ---------------------------------------------------------------------------


def _resolve_greeks(raw: dict) -> tuple[dict[str, Optional[float]], bool]:
    """Extract and validate all five Greeks from raw, enforcing null semantics.

    Rules (non-negotiable, Requirements 6.5):
    - If any Greek is absent or None → set to None; greeks_missing = True
    - If any Greek is 0.0 and not explicitly supplied as 0.0 by provider:
      this module cannot distinguish; to be safe, accept 0.0 only if the key
      is present.  (Zero greeks can legitimately exist, e.g. rho ≈ 0 for
      very short-dated options — we accept 0 only when the key is present.)
    - If ALL five Greeks are absent → greeks_missing = True

    Returns (greeks_dict, greeks_missing).
    """
    greek_keys = ("delta", "gamma", "theta", "vega", "rho")
    greeks: dict[str, Optional[float]] = {}
    any_missing = False

    for key in greek_keys:
        if not _is_genuinely_supplied(raw, key):
            greeks[key] = None
            any_missing = True
        else:
            val = _safe_float(raw[key], allow_zero=True)
            greeks[key] = val
            if val is None:
                any_missing = True

    # greeks_missing is True only when ALL five are absent/None
    all_missing = all(v is None for v in greeks.values())
    return greeks, all_missing


# ---------------------------------------------------------------------------
# Bid/ask null-semantic enforcement
# ---------------------------------------------------------------------------


def _resolve_bid_ask(raw: dict) -> tuple[Optional[float], Optional[float], bool]:
    """Extract bid and ask, enforcing null semantics.

    Rules (non-negotiable, Requirement 6.6):
    - Zero bid or ask is treated as not provided (placeholder zero prohibited).
    - If either field is absent or zero → bid_ask_missing = True for that
      field; however bid_ask_missing flag on the output record is True only
      when BOTH are missing.

    Returns (bid, ask, bid_ask_missing).
    """
    bid = _safe_float(raw.get("bid"), allow_zero=False) if _is_genuinely_supplied(raw, "bid") else None
    ask = _safe_float(raw.get("ask"), allow_zero=False) if _is_genuinely_supplied(raw, "ask") else None
    bid_ask_missing = bid is None and ask is None
    return bid, ask, bid_ask_missing


# ---------------------------------------------------------------------------
# IV null-semantic enforcement
# ---------------------------------------------------------------------------


def _resolve_iv(raw: dict, *, key: str = "iv") -> tuple[Optional[float], bool]:
    """Extract IV, enforcing null semantics.

    Rules (non-negotiable, Requirement 6.4):
    - Zero IV is NOT a valid substitute; if the value is 0.0 → iv = None,
      iv_missing = True.
    - Absent or None → iv = None, iv_missing = True.

    Returns (iv_value, iv_missing).
    """
    if not _is_genuinely_supplied(raw, key):
        return None, True

    val = _safe_float(raw[key], allow_zero=False)
    if val is None:
        # Either conversion failed or value was 0.0 (not a substitute)
        return None, True

    return val, False


# ---------------------------------------------------------------------------
# Normaliser class
# ---------------------------------------------------------------------------


class Normaliser:
    """Maps provider-specific raw responses to platform canonical form.

    Stateless — every method is pure with respect to the normaliser instance;
    no internal state is mutated.  A single shared instance can be used safely
    across async tasks.

    All three public methods return ``(normalised_dict, ok, incident)`` tuples
    matching the pipeline step contract.  When ``ok=True`` the incident is
    ``None``.  When ``ok=False`` a DataIncident-compatible dict is returned as
    the third element.

    Requirements: 6.1–6.12
    """

    def __init__(self, normalisation_version: str = NORMALISATION_VERSION) -> None:
        self._normalisation_version = normalisation_version

    # ------------------------------------------------------------------ #
    # Public: top-level dispatcher                                          #
    # ------------------------------------------------------------------ #

    def normalise(
        self,
        raw: dict,
        provider: str,
        data_type: str = "quote",
    ) -> tuple[dict, bool, Optional[dict]]:
        """Normalise a raw provider response based on ``data_type``.

        Parameters
        ----------
        raw        : Raw provider response dict.
        provider   : Provider identifier string (e.g. ``"angel_one"``).
        data_type  : ``"ohlcv"`` | ``"quote"`` | ``"option_chain_row"``
                     (default ``"quote"``).

        Returns
        -------
        ``(normalised, ok, incident)`` matching the pipeline step contract.
        """
        dispatch = {
            "ohlcv": self.normalise_ohlcv,
            "quote": self.normalise_quote,
            "option_chain_row": self.normalise_option_chain_row,
        }
        fn = dispatch.get(data_type, self.normalise_quote)
        return fn(raw, provider)

    # ------------------------------------------------------------------ #
    # Public: OHLCV                                                         #
    # ------------------------------------------------------------------ #

    def normalise_ohlcv(
        self,
        raw: dict,
        provider: str,
    ) -> tuple[dict, bool, Optional[dict]]:
        """Normalise a raw OHLCV candle bar from any provider.

        Required raw fields: ``open``, ``high``, ``low``, ``close``, ``time``
        (or ``timestamp`` / ``openTime`` variants — all resolved here).

        Missing ``volume`` → ``volume = 0``, ``volumeUnavailable = True``.
        Missing ``oi``    → ``oi = None``, ``oiMissing = True``.

        Requirements: 6.1, 6.2, 6.3, 6.9, 6.10
        """
        instrument_id = raw.get("instrumentId", raw.get("symbol", "UNKNOWN"))

        # ── Resolve required price fields ─────────────────────────────────
        open_price  = _safe_float(raw.get("open"))
        high_price  = _safe_float(raw.get("high"))
        low_price   = _safe_float(raw.get("low"))
        close_price = _safe_float(raw.get("close"))

        # Complete failure if any required price is absent/invalid
        if any(v is None for v in (open_price, high_price, low_price, close_price)):
            incident = _make_incident(
                incident_type="SCHEMA_VALIDATION",
                instrument_id=instrument_id,
                provider=provider,
                severity="HIGH",
                details={
                    "reason": "missing_required_ohlcv_price_fields",
                    "rawResponseHash": _raw_response_hash(raw),
                    "validationErrors": [
                        f"{k}=None" for k, v in {
                            "open": open_price,
                            "high": high_price,
                            "low": low_price,
                            "close": close_price,
                        }.items() if v is None
                    ],
                    "receivedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "provider": provider,
                    "endpoint": "ohlcv",
                },
            )
            logger.error(
                "normaliser_ohlcv_schema_validation_failed",
                component="normaliser",
                provider=provider,
                instrument_id=instrument_id,
                raw_response_hash=incident["details"]["rawResponseHash"],
            )
            return raw, False, incident

        # ── Resolve timestamp ─────────────────────────────────────────────
        # Accept "time", "timestamp", "openTime", "t" variants
        time_raw = raw.get("time") or raw.get("timestamp") or raw.get("openTime") or raw.get("t")
        if time_raw is None:
            time_val: Optional[int] = None
        else:
            time_val = _safe_int(time_raw)

        # ── Volume (partial-response: missing → 0 + flag) ─────────────────
        volume_unavailable = not _is_genuinely_supplied(raw, "volume")
        volume_val: int = 0
        if not volume_unavailable:
            v = _safe_int(raw["volume"])
            if v is not None and v >= 0:
                volume_val = v
            else:
                # Invalid volume — treat as unavailable
                volume_unavailable = True

        # ── OI (partial-response: missing/substituted → None + flag) ──────
        oi_val, oi_missing = _resolve_oi(raw)

        # ── Build normalised output ────────────────────────────────────────
        output: dict = {
            "time": time_val,
            "open": open_price,
            "high": high_price,
            "low": low_price,
            "close": close_price,
            "volume": volume_val,
            "volumeUnavailable": volume_unavailable,
            "oi": oi_val,
            "oiMissing": oi_missing,
            "provider": provider,
            "instrumentId": instrument_id,
            "normalisationVersion": self._normalisation_version,
        }

        # Copy through optional provenance fields if present
        for opt in ("exchange", "intervalStr", "interval", "sessionDate", "sourceType"):
            if opt in raw:
                output[opt] = raw[opt]

        _apply_metric_tags(output, _OHLCV_FIELD_TAGS)
        return output, True, None

    # ------------------------------------------------------------------ #
    # Public: Live Quote                                                    #
    # ------------------------------------------------------------------ #

    def normalise_quote(
        self,
        raw: dict,
        provider: str,
    ) -> tuple[dict, bool, Optional[dict]]:
        """Normalise a raw live quote snapshot from any provider.

        Required raw fields: ``ltp`` (last traded price).  All other fields
        are optional — missing ones are set to ``None`` with the appropriate
        missing flag.

        Requirements: 6.1–6.6, 6.9, 6.10, 3.2, 3.3
        """
        instrument_id = raw.get("instrumentId", raw.get("symbol", "UNKNOWN"))

        # ── Required: ltp ─────────────────────────────────────────────────
        ltp = _safe_float(raw.get("ltp"))
        if ltp is None:
            incident = _make_incident(
                incident_type="SCHEMA_VALIDATION",
                instrument_id=instrument_id,
                provider=provider,
                severity="HIGH",
                details={
                    "reason": "missing_required_field_ltp",
                    "rawResponseHash": _raw_response_hash(raw),
                    "validationErrors": ["ltp=None"],
                    "receivedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "provider": provider,
                    "endpoint": "quote",
                },
            )
            logger.error(
                "normaliser_quote_schema_validation_failed",
                component="normaliser",
                provider=provider,
                instrument_id=instrument_id,
            )
            return raw, False, incident

        # ── Optional price fields (partial-response: None on failure) ──────
        open_p   = _safe_float(raw.get("open"))
        high_p   = _safe_float(raw.get("high"))
        low_p    = _safe_float(raw.get("low"))
        prev_close = _safe_float(raw.get("prevClose"))

        # ── Derived: change / changePct ────────────────────────────────────
        change     = _safe_float(raw.get("change"))
        change_pct = _safe_float(raw.get("changePct"))

        # If not provided but we have ltp and prevClose → derive them
        if change is None and ltp is not None and prev_close is not None:
            change = ltp - prev_close
        if change_pct is None and change is not None and prev_close is not None and prev_close != 0.0:
            change_pct = round(change / prev_close * 100, 4)

        # ── Volume (missing → 0 + flag) ────────────────────────────────────
        volume_unavailable = not _is_genuinely_supplied(raw, "volume")
        volume_val: int = 0
        if not volume_unavailable:
            v = _safe_int(raw["volume"])
            if v is not None and v >= 0:
                volume_val = v
            else:
                volume_unavailable = True

        # ── OI (F&O only; null for equities) ──────────────────────────────
        oi_val, oi_missing = _resolve_oi(raw)

        # ── Bid / Ask (zero prohibited) ────────────────────────────────────
        bid, ask, bid_ask_missing = _resolve_bid_ask(raw)

        # ── TradedValue (always distinct from OI) ─────────────────────────
        traded_value = _safe_float(raw.get("tradedValue"))

        # ── Additional optional fields ─────────────────────────────────────
        total_buy_qty  = _safe_int(raw.get("totalBuyQty"))
        total_sell_qty = _safe_int(raw.get("totalSellQty"))
        upper_circuit  = _safe_float(raw.get("upperCircuit"))
        lower_circuit  = _safe_float(raw.get("lowerCircuit"))
        week_high_52   = _safe_float(raw.get("weekHigh52"))
        week_low_52    = _safe_float(raw.get("weekLow52"))
        last_trade_time = raw.get("lastTradeTime")
        market_status  = raw.get("marketStatus", "UNKNOWN")

        output: dict = {
            "instrumentId": instrument_id,
            "symbol": raw.get("symbol", instrument_id),
            "exchange": raw.get("exchange", "UNKNOWN"),
            "ltp": ltp,
            "open": open_p,
            "high": high_p,
            "low": low_p,
            "prevClose": prev_close,
            "change": change,
            "changePct": change_pct,
            "volume": volume_val,
            "volumeUnavailable": volume_unavailable,
            "oi": oi_val,
            "oiMissing": oi_missing,
            "tradedValue": traded_value,
            "totalBuyQty": total_buy_qty,
            "totalSellQty": total_sell_qty,
            "upperCircuit": upper_circuit,
            "lowerCircuit": lower_circuit,
            "weekHigh52": week_high_52,
            "weekLow52": week_low_52,
            "lastTradeTime": last_trade_time,
            "bid": bid,
            "ask": ask,
            "bidAskMissing": bid_ask_missing,
            "marketStatus": market_status,
            "provider": provider,
            "normalisationVersion": self._normalisation_version,
        }

        _apply_metric_tags(output, _QUOTE_FIELD_TAGS)
        return output, True, None

    # ------------------------------------------------------------------ #
    # Public: Option Chain Row                                              #
    # ------------------------------------------------------------------ #

    def normalise_option_chain_row(
        self,
        raw: dict,
        provider: str,
    ) -> tuple[dict, bool, Optional[dict]]:
        """Normalise a single option chain contract row from any provider.

        Required raw fields: ``strike``, ``optionType``.  All analytics
        fields (iv, Greeks) default to ``None`` with missing flags when
        absent.

        Requirements: 6.1–6.6, 6.9, 6.10, 3.7, 3.8
        """
        instrument_id = raw.get("instrumentId", raw.get("symbol", "UNKNOWN"))

        # ── Required: strike + optionType ─────────────────────────────────
        strike = _safe_float(raw.get("strike"))
        option_type = raw.get("optionType")

        if strike is None or option_type is None:
            incident = _make_incident(
                incident_type="SCHEMA_VALIDATION",
                instrument_id=instrument_id,
                provider=provider,
                severity="HIGH",
                details={
                    "reason": "missing_required_option_chain_fields",
                    "rawResponseHash": _raw_response_hash(raw),
                    "validationErrors": [
                        *( ["strike=None"] if strike is None else []),
                        *( ["optionType=None"] if option_type is None else []),
                    ],
                    "receivedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "provider": provider,
                    "endpoint": "option_chain",
                },
            )
            logger.error(
                "normaliser_option_chain_schema_validation_failed",
                component="normaliser",
                provider=provider,
                instrument_id=instrument_id,
            )
            return raw, False, incident

        # ── LTP (required for a meaningful row) ───────────────────────────
        ltp = _safe_float(raw.get("ltp"))

        # ── OI ────────────────────────────────────────────────────────────
        oi_val, oi_missing = _resolve_oi(raw)

        # ── OI Change ─────────────────────────────────────────────────────
        oi_change_missing = not _is_genuinely_supplied(raw, "oiChange")
        oi_change = _safe_int(raw.get("oiChange")) if not oi_change_missing else None

        # ── Volume ────────────────────────────────────────────────────────
        volume_missing = not _is_genuinely_supplied(raw, "volume")
        volume_val_opt: Optional[int] = None
        if not volume_missing:
            v = _safe_int(raw["volume"])
            volume_val_opt = v if v is not None and v >= 0 else None
            if volume_val_opt is None:
                volume_missing = True

        # ── TradedValue ───────────────────────────────────────────────────
        traded_value = _safe_float(raw.get("tradedValue"))

        # ── IV (zero prohibited) ──────────────────────────────────────────
        iv_val, iv_missing = _resolve_iv(raw)

        # ── Greeks (placeholder zeros prohibited) ─────────────────────────
        greeks, greeks_missing = _resolve_greeks(raw)

        # ── Bid / Ask (zero prohibited) ────────────────────────────────────
        bid, ask, bid_ask_missing = _resolve_bid_ask(raw)

        output: dict = {
            "instrumentId": instrument_id,
            "strike": strike,
            "optionType": option_type,
            "ltp": ltp,
            "bid": bid,
            "ask": ask,
            "bidAskMissing": bid_ask_missing,
            "oi": oi_val,
            "oiChange": oi_change,
            "volume": volume_val_opt,
            "tradedValue": traded_value,
            "iv": iv_val,
            "delta": greeks.get("delta"),
            "gamma": greeks.get("gamma"),
            "theta": greeks.get("theta"),
            "vega": greeks.get("vega"),
            "rho": greeks.get("rho"),
            "oiMissing": oi_missing,
            "oiChangeMissing": oi_change_missing,
            "volumeMissing": volume_missing,
            "ivMissing": iv_missing,
            "greeksMissing": greeks_missing,
            "provider": provider,
            "normalisationVersion": self._normalisation_version,
        }

        _apply_metric_tags(output, _OPTION_ROW_FIELD_TAGS)
        return output, True, None
