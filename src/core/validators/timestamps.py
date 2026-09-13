"""
Timestamp normalisation — pipeline step 4.

Converts all provider-supplied timestamps to UTC epoch milliseconds so that
every downstream component works in a single, unambiguous time domain.

Key rules (Requirements 6.7, 6.8, 17.3)
-----------------------------------------
- All internal storage/processing timestamps are UTC epoch milliseconds.
- All API response timestamps are UTC ISO-8601 strings with a ``Z`` suffix.
- IST interpretation occurs ONLY inside this module; all other modules use UTC.
- Timestamps that cannot be normalised cause the dataset to be rejected with a
  DataIncident containing the raw value, source identifier, and rejection reason.

Supported input formats
-----------------------
1. ISO-8601 string (with or without timezone offset / ``Z`` suffix).
2. Epoch seconds as ``int`` or ``float`` (values < 1e12 are treated as seconds;
   values ≥ 1e12 are treated as milliseconds already).
3. ``datetime.datetime`` object (with or without tzinfo).
4. ``datetime.date`` object (interpreted as midnight in the given timezone).
"""

from __future__ import annotations

import datetime
from typing import Any
from zoneinfo import ZoneInfo

__all__ = [
    "normalise_timestamp",
    "format_api_timestamp",
    "EXCHANGE_TIMEZONE_INDIAN",
    "EXCHANGE_TIMEZONE_CRYPTO",
]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

EXCHANGE_TIMEZONE_INDIAN: str = "Asia/Kolkata"
EXCHANGE_TIMEZONE_CRYPTO: str = "UTC"

# Threshold: values smaller than this are treated as *seconds*, not milliseconds.
# Unix epoch ms for year 2001-09-09 is 1_000_000_000_000; using 1e10 as the
# boundary means anything < 10 billion is assumed to be epoch seconds.
_EPOCH_MS_THRESHOLD: float = 1e10


def normalise_timestamp(
    raw_ts: Any,
    exchange_timezone: str = EXCHANGE_TIMEZONE_INDIAN,
) -> tuple[int, bool, str]:
    """Normalise a provider-supplied timestamp to UTC epoch milliseconds.

    Parameters
    ----------
    raw_ts:
        The raw timestamp value as received from the provider. Accepted types:
        - ``str`` — ISO-8601 with or without timezone designator, or a plain
          date string ``YYYY-MM-DD``.
        - ``int`` / ``float`` — epoch seconds (<1e10) or epoch ms (≥1e10).
        - ``datetime.datetime`` — with or without ``tzinfo``.
        - ``datetime.date`` — interpreted as midnight local time.
    exchange_timezone:
        IANA timezone name used to localise naive datetimes.  Defaults to
        ``"Asia/Kolkata"`` (IST) for Indian market data.  Pass ``"UTC"`` for
        crypto data.  **IST must only be used inside this module.**

    Returns
    -------
    ``(epoch_ms, success, error_message)``
    - On success: ``(non-negative int, True, "")``
    - On failure: ``(0, False, "<human-readable reason>")``
    """
    if raw_ts is None:
        return 0, False, "timestamp is None"

    tz = ZoneInfo(exchange_timezone)

    try:
        if isinstance(raw_ts, datetime.datetime):
            dt = _ensure_utc(raw_ts, tz)
            return _dt_to_epoch_ms(dt), True, ""

        if isinstance(raw_ts, datetime.date) and not isinstance(
            raw_ts, datetime.datetime
        ):
            # date → midnight in the exchange timezone
            dt = datetime.datetime(
                raw_ts.year, raw_ts.month, raw_ts.day, 0, 0, 0, tzinfo=tz
            )
            return _dt_to_epoch_ms(dt), True, ""

        if isinstance(raw_ts, (int, float)):
            return _numeric_to_epoch_ms(raw_ts), True, ""

        if isinstance(raw_ts, str):
            return _parse_string_ts(raw_ts, tz)

        return (
            0,
            False,
            f"unsupported timestamp type: {type(raw_ts).__name__!r}",
        )

    except Exception as exc:  # noqa: BLE001
        return 0, False, f"timestamp normalisation error: {exc}"


def format_api_timestamp(epoch_ms: int) -> str:
    """Format a UTC epoch-millisecond value as a UTC ISO-8601 string.

    The returned string always has a ``Z`` suffix and three decimal places
    for milliseconds, e.g. ``"2026-01-15T09:15:00.000Z"``.

    Parameters
    ----------
    epoch_ms:
        UTC epoch milliseconds.

    Returns
    -------
    UTC ISO-8601 string with ``Z`` suffix.
    """
    dt = datetime.datetime.fromtimestamp(
        epoch_ms / 1000.0, tz=datetime.timezone.utc
    )
    # Format: YYYY-MM-DDTHH:MM:SS.mmmZ
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _ensure_utc(
    dt: datetime.datetime, local_tz: ZoneInfo
) -> datetime.datetime:
    """Attach timezone info to a naive datetime, then convert to UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=local_tz)
    return dt.astimezone(datetime.timezone.utc)


def _dt_to_epoch_ms(dt: datetime.datetime) -> int:
    """Convert a timezone-aware UTC datetime to epoch milliseconds."""
    utc = dt.astimezone(datetime.timezone.utc)
    return int(utc.timestamp() * 1000)


def _numeric_to_epoch_ms(value: float) -> int:
    """Convert a numeric timestamp to epoch milliseconds.

    Values < 1e10 are assumed to be epoch *seconds*; values ≥ 1e10 are
    assumed to be epoch *milliseconds* already.
    """
    if value < _EPOCH_MS_THRESHOLD:
        return int(value * 1000)
    return int(value)


def _parse_string_ts(raw: str, tz: ZoneInfo) -> tuple[int, bool, str]:
    """Parse an ISO-8601 (or YYYY-MM-DD) string to epoch milliseconds."""
    raw = raw.strip()
    if not raw:
        return 0, False, "timestamp string is empty"

    # Replace space separator with 'T' for stdlib compatibility
    normalised = raw.replace(" ", "T")

    # Attempt stdlib fromisoformat (handles most ISO-8601 variants in 3.11+)
    try:
        dt = datetime.datetime.fromisoformat(normalised)
        dt = _ensure_utc(dt, tz)
        return _dt_to_epoch_ms(dt), True, ""
    except ValueError:
        pass

    # Attempt plain date
    try:
        d = datetime.date.fromisoformat(normalised)
        dt = datetime.datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=tz)
        return _dt_to_epoch_ms(dt), True, ""
    except ValueError:
        pass

    # Attempt numeric string
    try:
        numeric = float(normalised)
        return _numeric_to_epoch_ms(numeric), True, ""
    except ValueError:
        pass

    return (
        0,
        False,
        f"cannot parse timestamp string {raw!r}: not a recognised "
        "ISO-8601 format, plain date, or numeric value",
    )
