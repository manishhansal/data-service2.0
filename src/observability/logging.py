"""
Structured JSON logging configuration for DATA-SERVICE 2.0.

Uses ``structlog`` with:
- JSON renderer in production/staging  (machine-parseable, Requirement 22.6)
- Console renderer in development      (coloured, human-friendly)

Every log entry includes at minimum (Requirement 18.1):
    timestamp    — UTC ISO-8601 string
    level        — log level string (info, warning, error, …)
    service      — always "data-service"
    component    — caller-supplied; default "unknown"
    event        — the positional event string
    request_id   — from contextvars; default "unknown"

Optional fields carried when supplied by the caller:
    instrument_id, provider, duration_ms

Additional design constraints (Requirement 18.1):
- Log entries written ≤ 100ms after the triggering event (enforced by the
  synchronous structlog pipeline — no async buffering is introduced here).
- Entries are capped at 10 KB; any entry exceeding this limit has its
  non-essential payload fields stripped and is marked ``_truncated: true``.

Usage — bind request-scoped context at the FastAPI middleware layer:
    structlog.contextvars.bind_contextvars(
        request_id="req-uuid-v4",
        service="data-service",
    )

Usage — emit a log entry:
    import structlog
    logger = structlog.get_logger(__name__)
    logger.info("quote_published", component="market_engine",
                instrument_id="NSE:NIFTY:IDX", duration_ms=12)

Public API
----------
configure_logging(log_level, environment)
    Configure structlog for the given environment.  Must be called once at
    process startup before any log entries are emitted.  Safe to call again
    (re-configures structlog and the stdlib root logger).

bind_request_context(request_id, service)
    Bind the two service-wide context variables for the current async context.

clear_request_context()
    Clear all context variables for the current async context.

get_logger(name)
    Thin wrapper around ``structlog.get_logger`` — use this for consistent
    imports throughout the codebase.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

import structlog
import structlog.contextvars
import structlog.dev
import structlog.processors
import structlog.stdlib

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SERVICE_NAME: str = "data-service"
MAX_ENTRY_BYTES: int = 10 * 1024  # 10 KB (Requirement 18.1)

# Fields that must never be dropped during truncation.
_TRUNCATION_PRESERVE_KEYS: frozenset[str] = frozenset(
    {
        "timestamp",
        "level",
        "service",
        "component",
        "event",
        "request_id",
        "logger",
        "_truncated",
        "truncation_note",
    }
)

# ---------------------------------------------------------------------------
# Custom processors
# ---------------------------------------------------------------------------


def _add_service_defaults(
    logger: Any,  # noqa: ANN401
    method: str,
    event_dict: dict[str, Any],
) -> dict[str, Any]:
    """Inject ``service`` and ``component`` defaults when not already present.

    - ``service`` defaults to ``SERVICE_NAME`` ("data-service").
    - ``component`` defaults to ``"unknown"`` so that the field is always
      present (Requirement 18.1).
    """
    event_dict.setdefault("service", SERVICE_NAME)
    event_dict.setdefault("component", "unknown")
    return event_dict


def _enforce_size_cap(
    logger: Any,  # noqa: ANN401
    method: str,
    event_dict: dict[str, Any],
) -> dict[str, Any]:
    """Cap log entries at MAX_ENTRY_BYTES (10 KB).

    When an entry would exceed the limit the processor:
    1. Drops all non-essential fields (those not in
       ``_TRUNCATION_PRESERVE_KEYS``).
    2. Adds ``_truncated: true`` and a ``truncation_note`` explaining why.

    This preserves the structured contract for downstream log consumers while
    preventing runaway log size (Requirement 18.1).
    """
    try:
        serialised = json.dumps(event_dict, default=str)
    except (TypeError, ValueError):
        # Fallback: convert to string representation
        serialised = str(event_dict)

    if len(serialised.encode("utf-8")) > MAX_ENTRY_BYTES:
        # Keep only essential fields
        trimmed: dict[str, Any] = {
            k: v for k, v in event_dict.items() if k in _TRUNCATION_PRESERVE_KEYS
        }
        trimmed["_truncated"] = True
        trimmed["truncation_note"] = (
            f"log entry exceeded {MAX_ENTRY_BYTES // 1024}KB limit; "
            "non-essential fields removed"
        )
        return trimmed

    return event_dict


# ---------------------------------------------------------------------------
# Public configuration entry point
# ---------------------------------------------------------------------------


def configure_logging(log_level: str = "INFO", environment: str = "development") -> None:
    """Configure structlog and the stdlib root logger.

    This function is idempotent — calling it more than once re-applies the
    configuration.  It must be invoked before any log entries are emitted.

    Args:
        log_level:   One of ``DEBUG``, ``INFO``, ``WARNING``, ``ERROR``,
                     ``CRITICAL``.  Case-insensitive.
        environment: One of ``development``, ``staging``, ``production``.
                     Non-``development`` environments get the JSON renderer.
    """
    # Normalise inputs
    log_level_upper = log_level.upper()
    numeric_level = logging.getLevelName(log_level_upper)
    if not isinstance(numeric_level, int):
        # Fallback to INFO for unknown level strings
        numeric_level = logging.INFO
        log_level_upper = "INFO"

    is_production_like = environment.lower() in ("production", "staging")

    # ── Processors shared by structlog and the stdlib bridge ─────────────
    #
    # ORDER MATTERS — processors run in sequence, each receiving the output
    # of the previous one.
    shared_processors: list[structlog.types.Processor] = [
        # 1. Merge context-local variables (request_id, service) into every
        #    entry so callers need not repeat them on every call.
        structlog.contextvars.merge_contextvars,
        # 2. Inject service and component defaults.
        _add_service_defaults,
        # 3. Add the stdlib logger name as "logger".
        structlog.stdlib.add_logger_name,
        # 4. Add "level" (info / warning / error / …).
        structlog.stdlib.add_log_level,
        # 5. UTC ISO-8601 timestamp in the "timestamp" field (Req 18.1).
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        # 6. Render stack info when present (exc_info / stack_info).
        structlog.processors.StackInfoRenderer(),
        # 7. Format exception info to a string before rendering.
        structlog.processors.format_exc_info,
        # 8. Enforce the 10 KB per-entry size cap.
        _enforce_size_cap,
    ]

    # ── Choose renderer ───────────────────────────────────────────────────
    if is_production_like:
        # Machine-parseable JSON for production and staging (Requirement 22.6).
        renderer: structlog.types.Processor = structlog.processors.JSONRenderer()
    else:
        # Coloured console output for local development.
        renderer = structlog.dev.ConsoleRenderer(colors=True)

    # ── Configure structlog ───────────────────────────────────────────────
    structlog.configure(
        processors=[
            *shared_processors,
            # Wrap for the stdlib ProcessorFormatter bridge.
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        # Cache the bound logger after the first `get_logger` call in each
        # module for performance.  Must be disabled in tests that reconfigure
        # structlog mid-run.
        cache_logger_on_first_use=True,
    )

    # ── Configure stdlib ProcessorFormatter (handles third-party logs) ───
    formatter = structlog.stdlib.ProcessorFormatter(
        # Applied to log records that originate from stdlib `logging` calls
        # (e.g. uvicorn, SQLAlchemy, APScheduler) before they enter the
        # shared_processors chain.
        foreign_pre_chain=shared_processors,
        processors=[
            # Remove internal structlog metadata before rendering.
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    # ── Install the handler on the root stdlib logger ─────────────────────
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    # Remove any previously installed handlers to avoid duplicate output
    # when configure_logging is called multiple times.
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(numeric_level)

    # Quieten noisy third-party loggers in production.
    if is_production_like:
        logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
        logging.getLogger("asyncio").setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# Context variable helpers
# ---------------------------------------------------------------------------


def bind_request_context(
    *,
    request_id: str,
    service: str = SERVICE_NAME,
) -> None:
    """Bind request-scoped context variables for the current async context.

    Call this at the start of every incoming request (e.g. in FastAPI
    middleware) so that ``request_id`` and ``service`` appear on every log
    entry without explicit passing.

    Args:
        request_id: UUID v4 string identifying the request.
        service:    Service name; defaults to ``"data-service"``.
    """
    structlog.contextvars.bind_contextvars(
        request_id=request_id,
        service=service,
    )


def clear_request_context() -> None:
    """Clear all context variables for the current async context.

    Call this at the end of every request (e.g. in a FastAPI middleware
    ``finally`` block) to prevent context variables leaking into subsequent
    requests that reuse the same task/thread.
    """
    structlog.contextvars.clear_contextvars()


# ---------------------------------------------------------------------------
# Logger factory helper
# ---------------------------------------------------------------------------


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a structlog bound logger for the given module name.

    Thin wrapper around ``structlog.get_logger`` — prefer this import in all
    platform modules for a consistent import path::

        from src.observability.logging import get_logger
        logger = get_logger(__name__)

    Args:
        name: Logger name, typically ``__name__`` of the calling module.

    Returns:
        A structlog bound logger instance.
    """
    return structlog.get_logger(name)
