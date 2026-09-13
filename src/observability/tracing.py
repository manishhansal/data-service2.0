"""
src/observability/tracing.py

OpenTelemetry distributed tracing for DATA-SERVICE 2.0.

Initialises the SDK and exposes three span-emitting helpers that must be
used by every provider call, cache lookup, and pipeline step:

    - :func:`trace_provider_call`   — provider-level spans
    - :func:`trace_cache_lookup`    — cache-level spans
    - :func:`trace_pipeline_step`   — pipeline-step spans

Every span carries the standard MDS attributes:
    mds.provider, mds.instrumentId, mds.durationMs, mds.step, mds.cacheHit

Three exporters are selectable via the ``OTEL_EXPORTER`` environment variable
(default ``noop``) without code changes (Requirements 18.3, 22.7):

    - ``noop``   — no-op exporter for local development (default)
    - ``jaeger`` — exports via OTLP/gRPC to Jaeger's OTLP endpoint
                   (Jaeger 1.35+ accepts OTLP natively;
                    configure endpoint with OTEL_EXPORTER_JAEGER_ENDPOINT,
                    default: ``http://jaeger:4317``)
    - ``otlp``   — generic OTLP/gRPC exporter
                   (configure endpoint with OTEL_EXPORTER_OTLP_ENDPOINT,
                    default: ``http://otel-collector:4317``)

Spans are exported by a ``BatchSpanProcessor`` with a 5-second export
timeout (Requirement 18.3).

Design note — global provider singleton
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
OpenTelemetry 1.x allows the global ``TracerProvider`` to be set only once
per process (subsequent ``set_tracer_provider`` calls are silently ignored
after the first non-ProxyTracerProvider is installed).  Production code
calls :func:`configure_tracing` exactly once during application startup.
The span helpers accept an *optional* ``tracer_provider`` keyword argument
so that tests can pass a local, isolated provider without touching the
global state.
"""

from __future__ import annotations

import contextlib
import os
import time
from collections.abc import Generator
from typing import Optional

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource, SERVICE_NAME
from opentelemetry.sdk.trace import TracerProvider as SdkTracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SpanExporter,
)
from opentelemetry.trace import Span, SpanKind, StatusCode

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Span export timeout in milliseconds — must be ≤ 5 000 ms (Req 18.3).
_EXPORT_TIMEOUT_MS: int = 5_000

# Maximum time to wait between export attempts (ms).
_SCHEDULE_DELAY_MS: float = 2_000

_SERVICE_NAME = "data-service"


# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_tracer_provider: Optional[SdkTracerProvider] = None


# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------


def configure_tracing(otel_exporter: str = "noop") -> SdkTracerProvider:
    """Initialise the OpenTelemetry SDK and register it as the global provider.

    Called once during application startup.  Because OpenTelemetry 1.x
    silently ignores subsequent ``set_tracer_provider`` calls once a real
    provider is installed, this function stores the provider in the module
    variable ``_tracer_provider`` so :func:`shutdown_tracing` can reach it
    regardless of the global state.

    Args:
        otel_exporter: One of ``"noop"``, ``"jaeger"``, ``"otlp"``.
                       Matched case-insensitively.  Unknown values fall back
                       to ``noop`` with a warning.

    Returns:
        The configured :class:`opentelemetry.sdk.trace.TracerProvider`.
    """
    global _tracer_provider  # noqa: PLW0603

    resource = Resource.create({SERVICE_NAME: _SERVICE_NAME, "service.version": "2.0.0"})

    exporter = _build_exporter(otel_exporter.lower())

    provider = SdkTracerProvider(resource=resource)
    provider.add_span_processor(
        BatchSpanProcessor(
            exporter,
            export_timeout_millis=_EXPORT_TIMEOUT_MS,
            schedule_delay_millis=_SCHEDULE_DELAY_MS,
        )
    )

    # Register as the global OpenTelemetry provider.  This may be silently
    # ignored on the second call (OpenTelemetry 1.x restriction), which is
    # fine — we still return the newly-created provider.
    trace.set_tracer_provider(provider)

    _tracer_provider = provider
    return provider


def build_provider(otel_exporter: str = "noop") -> SdkTracerProvider:
    """Create and return a *standalone* ``SdkTracerProvider`` without touching
    the global OTel state.

    This is the entry point for unit tests and any code that needs an isolated
    provider.  Call :func:`configure_tracing` for the application singleton.

    Args:
        otel_exporter: Same values as :func:`configure_tracing`.

    Returns:
        A fully configured :class:`opentelemetry.sdk.trace.TracerProvider`
        that is **not** registered as the process-global provider.
    """
    resource = Resource.create({SERVICE_NAME: _SERVICE_NAME, "service.version": "2.0.0"})
    exporter = _build_exporter(otel_exporter.lower())
    provider = SdkTracerProvider(resource=resource)
    provider.add_span_processor(
        BatchSpanProcessor(
            exporter,
            export_timeout_millis=_EXPORT_TIMEOUT_MS,
            schedule_delay_millis=_SCHEDULE_DELAY_MS,
        )
    )
    return provider


def _build_exporter(exporter_name: str) -> SpanExporter:
    """Return the appropriate :class:`SpanExporter` for *exporter_name*.

    ``jaeger`` and ``otlp`` both use the OTLP/gRPC protocol — Jaeger 1.35+
    accepts OTLP natively on port 4317.  The only difference is which
    environment variable drives the endpoint default.

    Args:
        exporter_name: Normalised (lowercased) exporter identifier.

    Returns:
        A configured :class:`SpanExporter` instance.
    """
    if exporter_name == "otlp":
        return _build_otlp_exporter(
            default_endpoint="http://otel-collector:4317",
            endpoint_env_var="OTEL_EXPORTER_OTLP_ENDPOINT",
        )

    if exporter_name == "jaeger":
        # Modern Jaeger (1.35+) receives OTLP spans over gRPC on port 4317.
        # A separate ``opentelemetry-exporter-jaeger`` package is therefore
        # not required; we reuse the pinned OTLP exporter.
        return _build_otlp_exporter(
            default_endpoint="http://jaeger:4317",
            endpoint_env_var="OTEL_EXPORTER_JAEGER_ENDPOINT",
        )

    # Default: noop.
    if exporter_name != "noop":
        import structlog  # noqa: PLC0415

        structlog.get_logger(__name__).warning(
            "unknown_otel_exporter",
            requested=exporter_name,
            fallback="noop",
        )

    return _NoopSpanExporter()


def _build_otlp_exporter(default_endpoint: str, endpoint_env_var: str) -> SpanExporter:
    """Build an OTLP/gRPC span exporter.

    The endpoint is resolved from *endpoint_env_var* if set, otherwise
    *default_endpoint* is used.

    Args:
        default_endpoint: Fallback gRPC endpoint (``host:port``).
        endpoint_env_var: Environment variable that overrides the default.

    Returns:
        A configured :class:`OTLPSpanExporter`.
    """
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (  # noqa: PLC0415
        OTLPSpanExporter,
    )

    endpoint = os.environ.get(endpoint_env_var, default_endpoint)
    return OTLPSpanExporter(endpoint=endpoint)


def shutdown_tracing() -> None:
    """Flush pending spans and shut down the tracer provider gracefully.

    Called during application SIGTERM handling to ensure spans in-flight are
    exported before the process exits.
    """
    global _tracer_provider  # noqa: PLW0603

    if _tracer_provider is not None:
        _tracer_provider.force_flush(timeout_millis=_EXPORT_TIMEOUT_MS)
        _tracer_provider.shutdown()
        _tracer_provider = None


# ---------------------------------------------------------------------------
# Internal no-op exporter
# ---------------------------------------------------------------------------


class _NoopSpanExporter(SpanExporter):
    """A span exporter that discards all spans silently.

    Used when ``OTEL_EXPORTER=noop`` (local development / testing).
    """

    def export(self, spans: object) -> "SpanExporter.SpanExportResult":  # type: ignore[override]
        from opentelemetry.sdk.trace.export import SpanExportResult  # noqa: PLC0415

        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Tracer accessor
# ---------------------------------------------------------------------------


def get_tracer(
    name: str = _SERVICE_NAME,
    *,
    tracer_provider: Optional[SdkTracerProvider] = None,
) -> trace.Tracer:
    """Return a named tracer from *tracer_provider* or the global provider.

    Args:
        name:             Instrumentation library name.
        tracer_provider:  Optional explicit provider.  When ``None``, the
                          global OTel provider is used.  Pass an explicit
                          provider in tests to avoid touching global state.

    Returns:
        A :class:`opentelemetry.trace.Tracer`.
    """
    if tracer_provider is not None:
        return tracer_provider.get_tracer(name)
    return trace.get_tracer(name)


# ---------------------------------------------------------------------------
# Span helpers
# ---------------------------------------------------------------------------
#
# These three context managers are the *required* integration points.  Every
# provider call, cache lookup, and pipeline step MUST use one of them so that
# all spans carry the standard MDS attributes (Req 18.3).
#
# Each helper accepts an optional ``tracer_provider`` kwarg so that unit tests
# can inject an isolated provider with an InMemorySpanExporter without
# competing with the process-global provider.
#


@contextlib.contextmanager
def trace_provider_call(
    provider: str,
    operation: str,
    *,
    instrument_id: Optional[str] = None,
    tracer_provider: Optional[SdkTracerProvider] = None,
) -> Generator[Span, None, None]:
    """Context manager that wraps a provider HTTP/WebSocket call with a span.

    Sets the following span attributes:
        - ``mds.provider``       — provider identifier (e.g. ``"angel_one"``)
        - ``mds.instrumentId``   — canonical instrument ID (when known)
        - ``mds.step``           — fixed to ``"provider_call"``
        - ``mds.cacheHit``       — fixed to ``False`` (provider call = cache miss)
        - ``mds.durationMs``     — elapsed milliseconds (set on exit)

    Args:
        provider:          Provider identifier string.
        operation:         Short description of the call (e.g. ``"fetch_ohlcv"``).
        instrument_id:     Optional canonical instrument ID.
        tracer_provider:   Optional explicit provider (for testing).

    Yields:
        The active :class:`opentelemetry.trace.Span`.

    Example::

        async with trace_provider_call("angel_one", "fetch_ohlcv", instrument_id="NSE:NIFTY:IDX"):
            candles = await gateway.fetch(...)
    """
    tracer = get_tracer(tracer_provider=tracer_provider)
    span_name = f"provider.{provider}.{operation}"

    with tracer.start_as_current_span(span_name, kind=SpanKind.CLIENT) as span:
        _set_mds_attributes(
            span,
            provider=provider,
            instrument_id=instrument_id,
            step="provider_call",
            cache_hit=False,
        )
        t0 = time.monotonic()
        try:
            yield span
            span.set_status(StatusCode.OK)
        except Exception as exc:
            span.record_exception(exc)
            span.set_status(StatusCode.ERROR, str(exc))
            raise
        finally:
            duration_ms = (time.monotonic() - t0) * 1_000
            span.set_attribute("mds.durationMs", round(duration_ms, 3))


@contextlib.contextmanager
def trace_cache_lookup(
    cache_level: str,
    operation: str,
    *,
    provider: Optional[str] = None,
    instrument_id: Optional[str] = None,
    cache_hit: bool = False,
    tracer_provider: Optional[SdkTracerProvider] = None,
) -> Generator[Span, None, None]:
    """Context manager that wraps a cache read/write with a span.

    Sets the following span attributes:
        - ``mds.provider``       — originating provider (when known)
        - ``mds.instrumentId``   — canonical instrument ID (when known)
        - ``mds.step``           — ``"cache_<cache_level>"``  (e.g. ``"cache_l1"``)
        - ``mds.cacheHit``       — ``True`` on hit, ``False`` on miss
        - ``mds.durationMs``     — elapsed milliseconds (set on exit)

    Args:
        cache_level:       One of ``"l1"``, ``"l2"``, ``"l3"``.
        operation:         Short description (``"get"`` / ``"set"`` / ``"delete"``).
        provider:          Optional originating provider identifier.
        instrument_id:     Optional canonical instrument ID.
        cache_hit:         ``True`` when the lookup resulted in a cache hit.
        tracer_provider:   Optional explicit provider (for testing).

    Yields:
        The active :class:`opentelemetry.trace.Span`.

    Example::

        with trace_cache_lookup("l2", "get", instrument_id="NSE:NIFTY:IDX") as span:
            result = await redis_client.get(key)
            if result:
                span.set_attribute("mds.cacheHit", True)
    """
    tracer = get_tracer(tracer_provider=tracer_provider)
    span_name = f"cache.{cache_level}.{operation}"

    with tracer.start_as_current_span(span_name, kind=SpanKind.INTERNAL) as span:
        _set_mds_attributes(
            span,
            provider=provider,
            instrument_id=instrument_id,
            step=f"cache_{cache_level}",
            cache_hit=cache_hit,
        )
        t0 = time.monotonic()
        try:
            yield span
            span.set_status(StatusCode.OK)
        except Exception as exc:
            span.record_exception(exc)
            span.set_status(StatusCode.ERROR, str(exc))
            raise
        finally:
            duration_ms = (time.monotonic() - t0) * 1_000
            span.set_attribute("mds.durationMs", round(duration_ms, 3))


@contextlib.contextmanager
def trace_pipeline_step(
    step: str,
    *,
    provider: Optional[str] = None,
    instrument_id: Optional[str] = None,
    tracer_provider: Optional[SdkTracerProvider] = None,
) -> Generator[Span, None, None]:
    """Context manager that wraps a Validation Pipeline step with a span.

    Sets the following span attributes:
        - ``mds.provider``       — upstream provider (when known)
        - ``mds.instrumentId``   — canonical instrument ID (when known)
        - ``mds.step``           — pipeline step name (e.g. ``"normalise"``)
        - ``mds.cacheHit``       — fixed to ``False`` (N/A for pipeline steps)
        - ``mds.durationMs``     — elapsed milliseconds (set on exit)

    Args:
        step:              Pipeline step identifier string.
        provider:          Optional upstream provider identifier.
        instrument_id:     Optional canonical instrument ID.
        tracer_provider:   Optional explicit provider (for testing).

    Yields:
        The active :class:`opentelemetry.trace.Span`.

    Example::

        with trace_pipeline_step("normalise", provider="angel_one", instrument_id=inst_id):
            normalised = normaliser.normalise(raw_response)
    """
    tracer = get_tracer(tracer_provider=tracer_provider)
    span_name = f"pipeline.{step}"

    with tracer.start_as_current_span(span_name, kind=SpanKind.INTERNAL) as span:
        _set_mds_attributes(
            span,
            provider=provider,
            instrument_id=instrument_id,
            step=step,
            cache_hit=False,
        )
        t0 = time.monotonic()
        try:
            yield span
            span.set_status(StatusCode.OK)
        except Exception as exc:
            span.record_exception(exc)
            span.set_status(StatusCode.ERROR, str(exc))
            raise
        finally:
            duration_ms = (time.monotonic() - t0) * 1_000
            span.set_attribute("mds.durationMs", round(duration_ms, 3))


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _set_mds_attributes(
    span: Span,
    *,
    provider: Optional[str],
    instrument_id: Optional[str],
    step: str,
    cache_hit: bool,
) -> None:
    """Write the standard MDS span attributes if the span is recording.

    Non-recording spans (e.g. produced by the no-op tracer) ignore
    ``set_attribute`` calls silently, so this is always safe to call.

    Args:
        span:           Target span.
        provider:       Provider identifier string (``None`` → attribute omitted).
        instrument_id:  Canonical instrument ID (``None`` → attribute omitted).
        step:           Pipeline/cache/provider step label.
        cache_hit:      Whether this operation resulted in a cache hit.
    """
    if not span.is_recording():
        return

    if provider is not None:
        span.set_attribute("mds.provider", provider)
    if instrument_id is not None:
        span.set_attribute("mds.instrumentId", instrument_id)
    span.set_attribute("mds.step", step)
    span.set_attribute("mds.cacheHit", cache_hit)
