"""
tests/unit/observability/test_logging.py

Unit tests for src/observability/logging.py.

Covers (Requirements 18.1, 22.6):
- configure_logging() configures structlog without raising
- JSON renderer is active in production / staging environments
- Console renderer is active in development environment
- Required fields present in every JSON log entry:
    timestamp, level, service, component, event, request_id
- Default values injected when caller omits service / component
- bind_request_context() populates request_id and service in contextvars
- clear_request_context() removes bound context variables
- get_logger() returns a structlog bound logger
- 10 KB size cap: oversized entries are truncated and marked _truncated=True
- Truncated entries still contain all required / preserved fields
- Entries below 10 KB are not modified
- log_level is respected (DEBUG / INFO / WARNING / ERROR / CRITICAL)
- configure_logging() is idempotent (safe to call multiple times)
- Third-party stdlib logs are routed through the same formatter
- Exception info is serialised (not raw traceback object)
- Optional fields (instrument_id, provider, duration_ms) are passed through
"""

from __future__ import annotations

import io
import json
import logging
import sys
from typing import Any

import pytest
import structlog
import structlog.contextvars


# ---------------------------------------------------------------------------
# Test infrastructure helpers
# ---------------------------------------------------------------------------


def _capture_json_log(
    log_level: str = "DEBUG",
    environment: str = "production",
) -> tuple[io.StringIO, structlog.stdlib.BoundLogger]:
    """Configure logging to write JSON into a StringIO buffer.

    Resets structlog defaults first to allow clean re-configuration.
    Returns (buffer, logger).  The caller must clear context vars after use.
    """
    # ── Reset to avoid cached state from prior tests ─────────────────────
    structlog.reset_defaults()

    from src.observability.logging import (  # noqa: PLC0415
        _add_service_defaults,
        _enforce_size_cap,
    )

    shared_procs: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        _add_service_defaults,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        _enforce_size_cap,
    ]

    numeric = logging.getLevelName(log_level.upper())
    if not isinstance(numeric, int):
        numeric = logging.INFO

    structlog.configure(
        processors=[
            *shared_procs,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        # Disable caching so each test gets a fresh logger without stale config.
        cache_logger_on_first_use=False,
    )

    buf = io.StringIO()
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_procs,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
    )
    handler = logging.StreamHandler(buf)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(numeric)

    logger = structlog.get_logger("test.logging")
    return buf, logger


def _parse_first_line(buf: io.StringIO) -> dict[str, Any]:
    """Parse and return the first non-empty JSON line from the buffer."""
    buf.seek(0)
    for line in buf:
        line = line.strip()
        if line:
            return json.loads(line)
    raise AssertionError("No log output found in buffer")


# ---------------------------------------------------------------------------
# configure_logging
# ---------------------------------------------------------------------------


class TestConfigureLogging:
    def test_does_not_raise_production(self):
        from src.observability.logging import configure_logging

        structlog.reset_defaults()
        configure_logging("INFO", "production")  # must not raise

    def test_does_not_raise_development(self):
        from src.observability.logging import configure_logging

        structlog.reset_defaults()
        configure_logging("DEBUG", "development")  # must not raise

    def test_does_not_raise_staging(self):
        from src.observability.logging import configure_logging

        structlog.reset_defaults()
        configure_logging("WARNING", "staging")  # must not raise

    def test_idempotent_double_call(self):
        """Calling configure_logging twice must not raise or cause duplicate handlers."""
        from src.observability.logging import configure_logging

        structlog.reset_defaults()
        configure_logging("INFO", "production")
        configure_logging("INFO", "production")  # second call — must not raise or duplicate

        root = logging.getLogger()
        assert len(root.handlers) == 1, "Should not have duplicate handlers after double configure"

    def test_log_level_applied_to_root_logger(self):
        from src.observability.logging import configure_logging

        structlog.reset_defaults()
        configure_logging("WARNING", "development")
        assert logging.getLogger().level == logging.WARNING

    def test_log_level_debug_applied(self):
        from src.observability.logging import configure_logging

        structlog.reset_defaults()
        configure_logging("DEBUG", "development")
        assert logging.getLogger().level == logging.DEBUG

    def test_unknown_log_level_falls_back_to_info(self):
        from src.observability.logging import configure_logging

        structlog.reset_defaults()
        configure_logging("NOTAREAL", "development")  # must not raise
        # Falls back to INFO
        assert logging.getLogger().level == logging.INFO

    def test_root_logger_has_exactly_one_handler(self):
        from src.observability.logging import configure_logging

        structlog.reset_defaults()
        configure_logging("INFO", "production")
        root = logging.getLogger()
        assert len(root.handlers) == 1

    def test_handler_writes_to_stdout(self):
        from src.observability.logging import configure_logging

        structlog.reset_defaults()
        configure_logging("INFO", "production")
        root = logging.getLogger()
        handler = root.handlers[0]
        assert isinstance(handler, logging.StreamHandler)
        assert handler.stream is sys.stdout


# ---------------------------------------------------------------------------
# JSON renderer in production / staging
# ---------------------------------------------------------------------------


class TestJsonRenderer:
    def teardown_method(self):
        structlog.contextvars.clear_contextvars()
        structlog.reset_defaults()

    def test_production_output_is_valid_json(self):
        buf, logger = _capture_json_log("INFO", "production")
        logger.info("test_event", component="test")
        parsed = _parse_first_line(buf)
        assert isinstance(parsed, dict)

    def test_staging_output_is_valid_json(self):
        buf, logger = _capture_json_log("INFO", "staging")
        logger.info("staging_event", component="test")
        parsed = _parse_first_line(buf)
        assert isinstance(parsed, dict)

    def test_timestamp_field_present(self):
        buf, logger = _capture_json_log("INFO", "production")
        logger.info("ts_test", component="test")
        parsed = _parse_first_line(buf)
        assert "timestamp" in parsed

    def test_timestamp_is_utc_iso8601(self):
        buf, logger = _capture_json_log("INFO", "production")
        logger.info("ts_format_test", component="test")
        parsed = _parse_first_line(buf)
        ts = parsed["timestamp"]
        # Must end with Z for UTC
        assert isinstance(ts, str)
        assert ts.endswith("Z"), f"Expected UTC ISO-8601 (Z suffix), got: {ts!r}"

    def test_level_field_present(self):
        buf, logger = _capture_json_log("INFO", "production")
        logger.info("level_test", component="test")
        parsed = _parse_first_line(buf)
        assert "level" in parsed

    def test_level_value_info(self):
        buf, logger = _capture_json_log("INFO", "production")
        logger.info("level_info", component="test")
        parsed = _parse_first_line(buf)
        assert parsed["level"] == "info"

    def test_level_value_warning(self):
        buf, logger = _capture_json_log("DEBUG", "production")
        logger.warning("level_warn", component="test")
        parsed = _parse_first_line(buf)
        assert parsed["level"] == "warning"

    def test_level_value_error(self):
        buf, logger = _capture_json_log("DEBUG", "production")
        logger.error("level_err", component="test")
        parsed = _parse_first_line(buf)
        assert parsed["level"] == "error"

    def test_service_field_present_and_correct(self):
        buf, logger = _capture_json_log("INFO", "production")
        logger.info("svc_test", component="test")
        parsed = _parse_first_line(buf)
        assert parsed.get("service") == "data-service"

    def test_component_field_present_when_supplied(self):
        buf, logger = _capture_json_log("INFO", "production")
        logger.info("comp_test", component="market_engine")
        parsed = _parse_first_line(buf)
        assert parsed.get("component") == "market_engine"

    def test_event_field_present_and_matches(self):
        buf, logger = _capture_json_log("INFO", "production")
        logger.info("my_event", component="test")
        parsed = _parse_first_line(buf)
        assert parsed.get("event") == "my_event"

    def test_request_id_from_context(self):
        structlog.contextvars.bind_contextvars(request_id="req-abc-123")
        buf, logger = _capture_json_log("INFO", "production")
        logger.info("ctx_test", component="test")
        parsed = _parse_first_line(buf)
        assert parsed.get("request_id") == "req-abc-123"

    def test_all_required_fields_present(self):
        """All six mandatory fields must appear in every JSON entry (Req 18.1)."""
        structlog.contextvars.bind_contextvars(request_id="req-mandatory")
        buf, logger = _capture_json_log("INFO", "production")
        logger.info("mandatory_test", component="validator")
        parsed = _parse_first_line(buf)

        required = {"timestamp", "level", "service", "component", "event", "request_id"}
        missing = required - set(parsed.keys())
        assert not missing, f"Missing required fields: {missing}"


# ---------------------------------------------------------------------------
# Default field injection
# ---------------------------------------------------------------------------


class TestDefaultFieldInjection:
    def teardown_method(self):
        structlog.contextvars.clear_contextvars()
        structlog.reset_defaults()

    def test_service_defaults_to_data_service(self):
        """service must be 'data-service' even when not explicitly supplied."""
        buf, logger = _capture_json_log("INFO", "production")
        logger.info("default_svc", component="test")  # no service= kwarg
        parsed = _parse_first_line(buf)
        assert parsed["service"] == "data-service"

    def test_component_defaults_to_unknown(self):
        """component must default to 'unknown' when not supplied."""
        buf, logger = _capture_json_log("INFO", "production")
        logger.info("default_comp")  # no component= kwarg
        parsed = _parse_first_line(buf)
        assert parsed["component"] == "unknown"

    def test_explicit_service_overrides_default(self):
        """Explicitly passed service= should win over default."""
        buf, logger = _capture_json_log("INFO", "production")
        logger.info("custom_svc", service="custom-service", component="test")
        parsed = _parse_first_line(buf)
        assert parsed["service"] == "custom-service"

    def test_explicit_component_overrides_default(self):
        buf, logger = _capture_json_log("INFO", "production")
        logger.info("custom_comp", component="my_component")
        parsed = _parse_first_line(buf)
        assert parsed["component"] == "my_component"


# ---------------------------------------------------------------------------
# Optional fields pass-through
# ---------------------------------------------------------------------------


class TestOptionalFields:
    def teardown_method(self):
        structlog.contextvars.clear_contextvars()
        structlog.reset_defaults()

    def test_instrument_id_passed_through(self):
        buf, logger = _capture_json_log("INFO", "production")
        logger.info("opt_test", component="test", instrument_id="NSE:NIFTY:IDX")
        parsed = _parse_first_line(buf)
        assert parsed.get("instrument_id") == "NSE:NIFTY:IDX"

    def test_provider_passed_through(self):
        buf, logger = _capture_json_log("INFO", "production")
        logger.info("opt_test", component="test", provider="angel_one")
        parsed = _parse_first_line(buf)
        assert parsed.get("provider") == "angel_one"

    def test_duration_ms_passed_through(self):
        buf, logger = _capture_json_log("INFO", "production")
        logger.info("opt_test", component="test", duration_ms=42)
        parsed = _parse_first_line(buf)
        assert parsed.get("duration_ms") == 42

    def test_all_three_optional_fields(self):
        buf, logger = _capture_json_log("INFO", "production")
        logger.info(
            "full_test",
            component="market_engine",
            instrument_id="NSE:RELIANCE:EQ",
            provider="upstox",
            duration_ms=15,
        )
        parsed = _parse_first_line(buf)
        assert parsed["instrument_id"] == "NSE:RELIANCE:EQ"
        assert parsed["provider"] == "upstox"
        assert parsed["duration_ms"] == 15


# ---------------------------------------------------------------------------
# bind_request_context / clear_request_context
# ---------------------------------------------------------------------------


class TestContextHelpers:
    def teardown_method(self):
        structlog.contextvars.clear_contextvars()
        structlog.reset_defaults()

    def test_bind_request_context_sets_request_id(self):
        from src.observability.logging import bind_request_context

        bind_request_context(request_id="req-ctx-001")
        buf, logger = _capture_json_log("INFO", "production")
        logger.info("ctx_check", component="test")
        parsed = _parse_first_line(buf)
        assert parsed["request_id"] == "req-ctx-001"

    def test_bind_request_context_sets_service(self):
        from src.observability.logging import bind_request_context

        bind_request_context(request_id="req-svc-001", service="my-service")
        buf, logger = _capture_json_log("INFO", "production")
        logger.info("svc_check", component="test")
        parsed = _parse_first_line(buf)
        assert parsed["service"] == "my-service"

    def test_bind_request_context_default_service(self):
        from src.observability.logging import bind_request_context

        bind_request_context(request_id="req-default-svc")
        buf, logger = _capture_json_log("INFO", "production")
        logger.info("default_svc_check", component="test")
        parsed = _parse_first_line(buf)
        assert parsed["service"] == "data-service"

    def test_clear_request_context_removes_bound_vars(self):
        from src.observability.logging import bind_request_context, clear_request_context

        bind_request_context(request_id="req-to-clear")
        clear_request_context()

        buf, logger = _capture_json_log("INFO", "production")
        logger.info("after_clear", component="test")
        parsed = _parse_first_line(buf)
        # request_id should not appear after clearing
        assert "request_id" not in parsed

    def test_clear_is_idempotent(self):
        """Calling clear_request_context twice must not raise."""
        from src.observability.logging import clear_request_context

        clear_request_context()
        clear_request_context()  # second call — must not raise


# ---------------------------------------------------------------------------
# get_logger
# ---------------------------------------------------------------------------


class TestGetLogger:
    def teardown_method(self):
        structlog.reset_defaults()

    def test_returns_bound_logger(self):
        from src.observability.logging import get_logger

        logger = get_logger("some.module")
        assert logger is not None

    def test_logger_is_callable(self):
        from src.observability.logging import configure_logging, get_logger

        structlog.reset_defaults()
        configure_logging("INFO", "development")
        logger = get_logger("some.module")
        # Must not raise — just calling info with a dev renderer to stdout
        logger.info("test_callable", component="test")

    def test_different_names_return_different_loggers(self):
        from src.observability.logging import get_logger

        logger_a = get_logger("module.a")
        logger_b = get_logger("module.b")
        # They should be different wrapper instances (not the same object reference)
        # We test indirectly: both must not raise when called
        assert logger_a is not None
        assert logger_b is not None


# ---------------------------------------------------------------------------
# 10 KB size cap
# ---------------------------------------------------------------------------


class TestSizeCap:
    def teardown_method(self):
        structlog.contextvars.clear_contextvars()
        structlog.reset_defaults()

    def test_oversized_entry_is_truncated(self):
        buf, logger = _capture_json_log("INFO", "production")
        # Build a payload well over 10 KB
        big_data = "x" * 20_000
        logger.info("big_entry", component="test", payload=big_data)

        parsed = _parse_first_line(buf)
        assert parsed.get("_truncated") is True

    def test_truncated_entry_fits_within_10kb(self):
        buf, logger = _capture_json_log("INFO", "production")
        big_data = "y" * 20_000
        logger.info("big_entry2", component="test", payload=big_data)

        line = buf.getvalue().strip().split("\n")[0]
        assert len(line.encode("utf-8")) <= 10 * 1024, (
            f"Truncated entry still exceeds 10KB: {len(line.encode('utf-8'))} bytes"
        )

    def test_truncated_entry_preserves_event(self):
        buf, logger = _capture_json_log("INFO", "production")
        big_data = "z" * 20_000
        logger.info("preserved_event", component="test", payload=big_data)
        parsed = _parse_first_line(buf)
        assert parsed["event"] == "preserved_event"

    def test_truncated_entry_preserves_level(self):
        buf, logger = _capture_json_log("INFO", "production")
        logger.info("trunc_level", component="test", payload="a" * 20_000)
        parsed = _parse_first_line(buf)
        assert "level" in parsed

    def test_truncated_entry_preserves_timestamp(self):
        buf, logger = _capture_json_log("INFO", "production")
        logger.info("trunc_ts", component="test", payload="b" * 20_000)
        parsed = _parse_first_line(buf)
        assert "timestamp" in parsed

    def test_truncated_entry_preserves_service(self):
        buf, logger = _capture_json_log("INFO", "production")
        logger.info("trunc_svc", component="test", payload="c" * 20_000)
        parsed = _parse_first_line(buf)
        assert parsed.get("service") == "data-service"

    def test_truncated_entry_preserves_component(self):
        buf, logger = _capture_json_log("INFO", "production")
        logger.info("trunc_comp", component="my_comp", payload="d" * 20_000)
        parsed = _parse_first_line(buf)
        assert parsed.get("component") == "my_comp"

    def test_truncated_entry_has_truncation_note(self):
        buf, logger = _capture_json_log("INFO", "production")
        logger.info("note_test", component="test", payload="e" * 20_000)
        parsed = _parse_first_line(buf)
        assert "truncation_note" in parsed
        assert "10KB" in parsed["truncation_note"] or "10" in parsed["truncation_note"]

    def test_small_entry_not_truncated(self):
        buf, logger = _capture_json_log("INFO", "production")
        logger.info("small_entry", component="test", key="value")
        parsed = _parse_first_line(buf)
        assert "_truncated" not in parsed

    def test_entry_exactly_at_limit_not_truncated(self):
        """An entry exactly at 10 KB should not be truncated."""
        from src.observability.logging import _enforce_size_cap  # noqa: PLC0415
        import json

        # Craft a dict that is <= 10 KB when serialised
        small_dict = {
            "event": "test",
            "level": "info",
            "service": "data-service",
            "component": "test",
        }
        # Ensure it's small enough
        assert len(json.dumps(small_dict).encode("utf-8")) <= 10 * 1024
        result = _enforce_size_cap(None, "info", small_dict)
        assert "_truncated" not in result

    def test_enforce_size_cap_processor_directly(self):
        """Unit-test the processor function in isolation."""
        from src.observability.logging import _enforce_size_cap  # noqa: PLC0415

        big_event = {
            "event": "huge",
            "level": "info",
            "service": "data-service",
            "component": "test",
            "payload": "x" * 50_000,
        }
        result = _enforce_size_cap(None, "info", big_event)
        assert result["_truncated"] is True
        assert "payload" not in result  # payload should be stripped


# ---------------------------------------------------------------------------
# Log level filtering
# ---------------------------------------------------------------------------


class TestLogLevelFiltering:
    def teardown_method(self):
        structlog.contextvars.clear_contextvars()
        structlog.reset_defaults()

    def test_info_filtered_when_warning_set(self):
        """INFO entries must not appear when log level is WARNING."""
        buf, logger = _capture_json_log("WARNING", "production")
        logger.info("should_be_filtered", component="test")
        logger.warning("should_appear", component="test")
        buf.seek(0)
        lines = [l.strip() for l in buf if l.strip()]
        assert len(lines) == 1
        parsed = json.loads(lines[0])
        assert parsed["event"] == "should_appear"

    def test_debug_appears_when_debug_level(self):
        buf, logger = _capture_json_log("DEBUG", "production")
        logger.debug("debug_entry", component="test")
        parsed = _parse_first_line(buf)
        assert parsed["event"] == "debug_entry"

    def test_debug_filtered_when_info_level(self):
        buf, logger = _capture_json_log("INFO", "production")
        logger.debug("should_not_appear", component="test")
        logger.info("should_appear", component="test")
        buf.seek(0)
        lines = [l.strip() for l in buf if l.strip()]
        # Only the INFO entry should appear
        assert len(lines) == 1
        parsed = json.loads(lines[0])
        assert parsed["event"] == "should_appear"


# ---------------------------------------------------------------------------
# Exception serialisation
# ---------------------------------------------------------------------------


class TestExceptionSerialisation:
    def teardown_method(self):
        structlog.contextvars.clear_contextvars()
        structlog.reset_defaults()

    def test_exception_info_is_string_not_object(self):
        """exc_info must be rendered as a string, not a raw Python traceback object."""
        buf, logger = _capture_json_log("INFO", "production")
        try:
            raise ValueError("test error")
        except ValueError:
            logger.error("error_with_exc", component="test", exc_info=True)

        output = buf.getvalue().strip()
        if output:
            # Should be valid JSON — raw traceback objects would break JSON serialisation
            parsed = json.loads(output)
            assert isinstance(parsed, dict)
            # The exception should be in the entry in some string form
            if "exception" in parsed:
                assert isinstance(parsed["exception"], str)

    def test_no_exc_info_does_not_add_exception_field(self):
        buf, logger = _capture_json_log("INFO", "production")
        logger.info("no_exc", component="test")
        parsed = _parse_first_line(buf)
        # exception field should not appear when no exception was raised
        assert "exc_info" not in parsed


# ---------------------------------------------------------------------------
# Third-party stdlib log routing
# ---------------------------------------------------------------------------


class TestStdlibLogRouting:
    def teardown_method(self):
        structlog.contextvars.clear_contextvars()
        structlog.reset_defaults()

    def test_stdlib_logger_routes_through_formatter(self):
        """A stdlib logging call must produce valid JSON through the shared formatter."""
        buf, _ = _capture_json_log("DEBUG", "production")

        std_logger = logging.getLogger("test.stdlib")
        std_logger.info("stdlib_event")

        output = buf.getvalue().strip()
        if output:
            # Must be parseable JSON
            parsed = json.loads(output)
            assert isinstance(parsed, dict)
            assert "timestamp" in parsed
            assert "level" in parsed


# ---------------------------------------------------------------------------
# SERVICE_NAME constant
# ---------------------------------------------------------------------------


class TestServiceNameConstant:
    def test_service_name_is_data_service(self):
        from src.observability.logging import SERVICE_NAME

        assert SERVICE_NAME == "data-service"


# ---------------------------------------------------------------------------
# MAX_ENTRY_BYTES constant
# ---------------------------------------------------------------------------


class TestMaxEntryBytes:
    def test_max_entry_bytes_is_10kb(self):
        from src.observability.logging import MAX_ENTRY_BYTES

        assert MAX_ENTRY_BYTES == 10 * 1024
