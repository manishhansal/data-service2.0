"""
src/core/shell_safety.py

Shell command safety and log-injection prevention utilities.

This module provides helpers that ensure provider-supplied values (symbol
names, date strings, query parameters) cannot be used for:

- **Log injection** — inserting newlines, carriage returns, or ANSI escape
  sequences into structured log entries to forge or corrupt log output.
- **Shell injection** — embedding shell-special characters in strings that
  are later interpolated into shell commands or HTTP request parameters.

All functions are pure (no I/O) and safe to call from any context.

Requirement: 19.5
"""

from __future__ import annotations

import re
import shlex

# ---------------------------------------------------------------------------
# Log-injection sanitisation
# ---------------------------------------------------------------------------

# Characters and sequences that can corrupt a structured log line.
# We strip:
#   \n  — newline (log line separator in most log processors)
#   \r  — carriage return
#   \x00 — null byte (terminates C strings, confuses parsers)
#   ANSI CSI escape sequences: ESC [ ... <letter>  (e.g. colours, cursor moves)
#   OSC sequences: ESC ] ... ST  (e.g. terminal title injection)
#   Any remaining bare ESC (0x1b) after sequence removal
_ANSI_ESCAPE_RE = re.compile(
    r"""
    \x1b          # ESC character
    (?:
        \[[0-?]*[ -/]*[@-~]   # CSI sequences  (ESC [ ... final byte)
      | \][^\x1b\x07]*(?:\x07|\x1b\\)  # OSC sequences (ESC ] ... BEL/ST)
      | [@-Z\\-_]             # Two-character escape sequences
    )
    """,
    re.VERBOSE,
)


def sanitize_for_log(value: str) -> str:
    """Return *value* with log-injection characters removed.

    Strips:
    - Newlines (``\\n``) and carriage returns (``\\r``) — prevent log-line
      splitting attacks.
    - Null bytes (``\\x00``) — prevent log parser confusion.
    - ANSI / VT100 escape sequences — prevent terminal / log-viewer
      manipulation.

    The returned string is safe to embed in a structured JSON log field.
    Non-printable characters not covered above are replaced with ``?``.

    Args:
        value: The raw string value to sanitise (e.g. a provider-supplied
               symbol name or query parameter).

    Returns:
        A sanitised string that contains no log-injection characters.

    Examples::

        >>> sanitize_for_log("NIFTY")
        'NIFTY'
        >>> sanitize_for_log("bad\\nvalue")
        'badvalue'
        >>> sanitize_for_log("esc\\x1b[31mred")
        'escred'
    """
    if not isinstance(value, str):
        value = str(value)

    # 1. Strip ANSI / VT escape sequences first (before stripping bare ESC).
    cleaned = _ANSI_ESCAPE_RE.sub("", value)

    # 2. Remove any remaining bare ESC characters.
    cleaned = cleaned.replace("\x1b", "")

    # 3. Remove newlines, carriage returns, and null bytes.
    cleaned = cleaned.replace("\n", "").replace("\r", "").replace("\x00", "")

    # 4. Replace any remaining non-printable ASCII characters with '?'.
    #    Printable range: 0x20 (space) through 0x7e (~), plus anything ≥ 0x80
    #    (multibyte Unicode is fine).
    sanitised_chars = []
    for ch in cleaned:
        cp = ord(ch)
        if cp < 0x20 or cp == 0x7F:
            sanitised_chars.append("?")
        else:
            sanitised_chars.append(ch)

    return "".join(sanitised_chars)


# ---------------------------------------------------------------------------
# Shell command safety
# ---------------------------------------------------------------------------


def quote_shell_arg(value: str) -> str:
    """Return *value* safely quoted for inclusion in a POSIX shell command.

    Uses :func:`shlex.quote` which wraps the value in single quotes and
    escapes any embedded single quotes.  The result can be safely embedded
    in a shell command string without risk of injection.

    This must be used whenever a provider-supplied value (symbol, date,
    query parameter) is passed to a subprocess or assembled into a shell
    command string.  Prefer parameterised subprocess calls (``subprocess.run``
    with a list of args) over string-assembled commands; use this function
    only when string assembly is unavoidable.

    Args:
        value: Raw string to quote (e.g. a symbol name, date string, or
               query parameter value).

    Returns:
        A single-quoted shell-safe string.

    Examples::

        >>> quote_shell_arg("NIFTY")
        "'NIFTY'"
        >>> quote_shell_arg("rm -rf /")
        "'rm -rf /'"
        >>> quote_shell_arg("it's a test")
        "'it'\"'\"'s a test'"
    """
    if not isinstance(value, str):
        value = str(value)
    return shlex.quote(value)
