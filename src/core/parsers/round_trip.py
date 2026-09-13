"""
Round-Trip Parser Property Enforcement — Task 5.9

Provides a reusable utility for verifying the round-trip property:
    ``parse(serialise(parse(raw))) == parse(raw)``

This property ensures that the canonical representation produced by a parser
is stable across serialise/parse cycles — a key correctness invariant for all
parsers in the platform (Requirements 4.11, 17.8).

Public API
----------
verify_round_trip(raw, parser, serialiser)  → tuple[bool, str]
    Functional interface for one-off checks.

RoundTripProperty                           — class
    Encapsulates a parser/serialiser pair; exposes ``check(raw)`` → RoundTripResult.

Parser-specific stubs (used by the validation pipeline step 3 and test suite):
    json_parser         — JSON dict passthrough parser
    json_serialiser     — JSON dict passthrough serialiser
    protobuf_parser     — placeholder (Upstox Protobuf)
    protobuf_serialiser — placeholder
    bhavcopy_parser     — placeholder (NSE bhavcopy CSV)
    bhavcopy_serialiser — placeholder
    nse_charting_parser     — placeholder (NSE charting response)
    nse_charting_serialiser — placeholder

Requirements: 4.11, 17.8
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Any, Callable, Optional

from src.observability.logging import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RoundTripResult:
    """Result of a single round-trip property check.

    Attributes
    ----------
    ok      : True when the property holds; False otherwise.
    error   : Empty string when ok is True; human-readable explanation when False.
    raw     : The input that was checked (preserved for diagnostics).
    first_parse  : Result of ``parse(raw)`` — may be None for invalid input.
    second_parse : Result of ``parse(serialise(parse(raw)))`` — may be None.
    """

    ok: bool
    error: str
    raw: Any = None
    first_parse: Any = None
    second_parse: Any = None


# ---------------------------------------------------------------------------
# Core functional interface
# ---------------------------------------------------------------------------


def verify_round_trip(
    raw: Any,
    parser: Callable[[Any], Optional[Any]],
    serialiser: Callable[[Any], Any],
) -> tuple[bool, str]:
    """Verify the round-trip property for a given ``raw`` input.

    The property checked:
        ``parse(serialise(parse(raw))) == parse(raw)``

    Parameters
    ----------
    raw        : The raw input (string, dict, bytes, etc.) to parse.
    parser     : Callable that maps raw → canonical (returns None on failure).
    serialiser : Callable that maps canonical → serialised form suitable for
                 re-parsing with ``parser``.

    Returns
    -------
    ``(True, "")`` when the property holds.
    ``(False, error_message)`` when it does not, with a diagnostic message.

    Semantics for None-returning parsers
    -------------------------------------
    If ``parse(raw)`` returns ``None``, the input is unparseable.  The property
    trivially holds (both sides are ``None``), but we return ``(False, ...)``
    to surface the parse failure as a real problem.

    Requirements: 4.11, 17.8
    """
    # Step 1: first parse
    try:
        first_parse = parser(raw)
    except Exception as exc:
        msg = f"round_trip: parser raised exception on raw input: {exc}"
        logger.warning(
            "round_trip_parser_exception",
            component="round_trip",
            error=str(exc),
        )
        return False, msg

    if first_parse is None:
        msg = "round_trip: parse(raw) returned None — input is unparseable"
        return False, msg

    # Step 2: serialise the first parse
    try:
        serialised = serialiser(first_parse)
    except Exception as exc:
        msg = f"round_trip: serialiser raised exception: {exc}"
        logger.warning(
            "round_trip_serialiser_exception",
            component="round_trip",
            error=str(exc),
        )
        return False, msg

    # Step 3: parse the serialised form
    try:
        second_parse = parser(serialised)
    except Exception as exc:
        msg = f"round_trip: parser raised exception on re-parsed input: {exc}"
        logger.warning(
            "round_trip_reparse_exception",
            component="round_trip",
            error=str(exc),
        )
        return False, msg

    # Step 4: compare
    if first_parse == second_parse:
        return True, ""

    msg = (
        f"round_trip: parse(serialise(parse(raw))) != parse(raw)\n"
        f"  first_parse:  {first_parse!r}\n"
        f"  second_parse: {second_parse!r}"
    )
    logger.warning(
        "round_trip_property_violated",
        component="round_trip",
        first_parse=repr(first_parse),
        second_parse=repr(second_parse),
    )
    return False, msg


# ---------------------------------------------------------------------------
# Class-based interface
# ---------------------------------------------------------------------------


class RoundTripProperty:
    """Encapsulates a parser/serialiser pair for repeated round-trip checks.

    Usage
    -----
    ::

        from src.core.parsers.deribit_parser import parse, serialise
        prop = RoundTripProperty(parser=parse, serialiser=serialise)
        result = prop.check("BTC-27JAN23-20000-C")
        assert result.ok

    Parameters
    ----------
    parser     : Callable that maps raw input → canonical (None on failure).
    serialiser : Callable that maps canonical → a form re-parseable by parser.
    name       : Optional human-readable name for logging/diagnostics.
    """

    def __init__(
        self,
        parser: Callable[[Any], Optional[Any]],
        serialiser: Callable[[Any], Any],
        name: str = "unnamed",
    ) -> None:
        self._parser = parser
        self._serialiser = serialiser
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    def check(self, raw: Any) -> RoundTripResult:
        """Check the round-trip property for ``raw``.

        Returns a :class:`RoundTripResult` with ``ok``, ``error``, and
        the intermediate parse results for diagnostics.
        """
        # Step 1: first parse
        try:
            first_parse = self._parser(raw)
        except Exception as exc:
            return RoundTripResult(
                ok=False,
                error=f"[{self._name}] parser raised on raw: {exc}",
                raw=raw,
                first_parse=None,
                second_parse=None,
            )

        if first_parse is None:
            return RoundTripResult(
                ok=False,
                error=f"[{self._name}] parse(raw) returned None — unparseable",
                raw=raw,
                first_parse=None,
                second_parse=None,
            )

        # Step 2: serialise
        try:
            serialised = self._serialiser(first_parse)
        except Exception as exc:
            return RoundTripResult(
                ok=False,
                error=f"[{self._name}] serialiser raised: {exc}",
                raw=raw,
                first_parse=first_parse,
                second_parse=None,
            )

        # Step 3: parse again
        try:
            second_parse = self._parser(serialised)
        except Exception as exc:
            return RoundTripResult(
                ok=False,
                error=f"[{self._name}] parser raised on re-parsed input: {exc}",
                raw=raw,
                first_parse=first_parse,
                second_parse=None,
            )

        # Step 4: compare
        if first_parse == second_parse:
            return RoundTripResult(
                ok=True,
                error="",
                raw=raw,
                first_parse=first_parse,
                second_parse=second_parse,
            )

        return RoundTripResult(
            ok=False,
            error=(
                f"[{self._name}] round-trip violated: "
                f"parse(serialise(parse(raw))) != parse(raw)"
            ),
            raw=raw,
            first_parse=first_parse,
            second_parse=second_parse,
        )


# ---------------------------------------------------------------------------
# Parser stubs for each platform parser type
# ---------------------------------------------------------------------------
# These stubs satisfy the requirement that round-trip is enforced in the test
# suite for each parser type (Requirement 17.8).  The JSON stub has a real
# implementation; the rest are placeholders that will be wired with the actual
# parser implementations when those are completed.


# ── JSON parser / serialiser ─────────────────────────────────────────────


def json_parser(raw: Any) -> Optional[dict]:
    """Parse a JSON string or dict into a Python dict.

    - If ``raw`` is already a dict, returns a deep copy.
    - If ``raw`` is a JSON string, deserialises it.
    - Returns None on failure.
    """
    if isinstance(raw, dict):
        return copy.deepcopy(raw)
    if isinstance(raw, (str, bytes)):
        try:
            result = json.loads(raw)
            if isinstance(result, dict):
                return result
            return None
        except (json.JSONDecodeError, ValueError):
            return None
    return None


def json_serialiser(canonical: dict) -> str:
    """Serialise a canonical dict to a JSON string (sorted keys for stability)."""
    return json.dumps(canonical, sort_keys=True, default=str)


# ── Upstox Protobuf stub ─────────────────────────────────────────────────


def protobuf_parser(raw: Any) -> Optional[dict]:
    """Placeholder parser for Upstox V3 Protobuf messages.

    This will be replaced with the actual Protobuf decoder when the
    Upstox streaming adapter is implemented (Phase 8 / Task 8.3).

    Currently returns the input as-is if it is already a dict (for testing
    round-trip infrastructure without a real Protobuf decoder).
    """
    # TODO: replace with actual Protobuf decode when Task 8.3 is implemented
    if isinstance(raw, dict):
        return copy.deepcopy(raw)
    logger.warning(
        "protobuf_parser_placeholder_called",
        component="round_trip",
        reason="real_protobuf_parser_not_yet_implemented",
    )
    return None


def protobuf_serialiser(canonical: dict) -> dict:
    """Placeholder serialiser for Upstox V3 Protobuf messages.

    Returns the canonical dict unchanged until the real implementation
    is wired in from Task 8.3.
    """
    # TODO: replace with actual Protobuf encode when Task 8.3 is implemented
    return copy.deepcopy(canonical)


# ── NSE bhavcopy CSV stub ────────────────────────────────────────────────


def bhavcopy_parser(raw: Any) -> Optional[dict]:
    """Placeholder parser for NSE bhavcopy CSV rows.

    This will be replaced with the actual CSV parser when the bhavcopy
    ingestion adapter is implemented.

    Currently returns the input as-is if it is already a dict.
    """
    # TODO: replace with actual bhavcopy CSV parser
    if isinstance(raw, dict):
        return copy.deepcopy(raw)
    logger.warning(
        "bhavcopy_parser_placeholder_called",
        component="round_trip",
        reason="real_bhavcopy_parser_not_yet_implemented",
    )
    return None


def bhavcopy_serialiser(canonical: dict) -> dict:
    """Placeholder serialiser for NSE bhavcopy CSV rows."""
    # TODO: replace with actual bhavcopy CSV serialiser
    return copy.deepcopy(canonical)


# ── NSE charting response stub ───────────────────────────────────────────


def nse_charting_parser(raw: Any) -> Optional[dict]:
    """Placeholder parser for NSE charting API responses.

    This will be replaced with the actual NSE charting response parser
    when the Scrapling/NSE adapter is fully implemented (Task 4.5).
    """
    # TODO: replace with actual NSE charting response parser
    if isinstance(raw, dict):
        return copy.deepcopy(raw)
    logger.warning(
        "nse_charting_parser_placeholder_called",
        component="round_trip",
        reason="real_nse_charting_parser_not_yet_implemented",
    )
    return None


def nse_charting_serialiser(canonical: dict) -> dict:
    """Placeholder serialiser for NSE charting API responses."""
    # TODO: replace with actual NSE charting response serialiser
    return copy.deepcopy(canonical)


# ---------------------------------------------------------------------------
# Pre-built RoundTripProperty instances for use in validation pipeline
# ---------------------------------------------------------------------------

JSON_ROUND_TRIP = RoundTripProperty(
    parser=json_parser,
    serialiser=json_serialiser,
    name="json",
)

PROTOBUF_ROUND_TRIP = RoundTripProperty(
    parser=protobuf_parser,
    serialiser=protobuf_serialiser,
    name="protobuf",
)

BHAVCOPY_ROUND_TRIP = RoundTripProperty(
    parser=bhavcopy_parser,
    serialiser=bhavcopy_serialiser,
    name="bhavcopy",
)

NSE_CHARTING_ROUND_TRIP = RoundTripProperty(
    parser=nse_charting_parser,
    serialiser=nse_charting_serialiser,
    name="nse_charting",
)
