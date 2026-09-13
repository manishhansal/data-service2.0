"""
tests/unit/observability/test_tracing.py

Unit tests for src/observability/tracing.py.

Design note — isolated providers
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
OpenTelemetry 1.x only allows setting the global ``TracerProvider`` once.
Tests therefore use ``build_provider()`` (which does NOT touch the global
state) plus ``SimpleSpanProcessor`` + ``InMemorySpanExporter`` to get
synchronous, isolated access to completed spans.

Each test class creates a fresh provider/exporter pair in ``setup_method``
and passes it explicitly to the span helpers via the ``tracer_provider``
keyword argument.

Covers:
- configure_tracing() returns a SdkTracerProvider for each exporter type
- build_provider() returns an isolated SdkTracerProvider
- _NoopSpanExporter discards spans silently
- Span helpers (trace_provider_call, trace_cache_lookup, trace_pipeline_step):
    - emit spans with all required mds.* attributes
    - set mds.durationMs to a non-negative float
    - propagate exceptions and set ERROR status
    - work correctly when no optional args are supplied
- shutdown_tracing() is idempotent
- get_tracer() works with explicit provider
"""

from __future__ import annotations

import time

import pytest
from opentelemetry.sdk.trace import TracerProvider as SdkTracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _isolated_provider() -> tuple[SdkTracerProvider, InMemorySpanExporter]:
    """Return an isolated provider + exporter pair WITHOUT touching the global OTel state.

    Uses ``SimpleSpanProcessor`` for synchronous span delivery (no batching
    delay in tests).
    """
    from opentelemetry.sdk.resources import Resource, SERVICE_NAME  # noqa: PLC0415

    exporter = InMemorySpanExporter()
    resource = Resource.create({SERVICE_NAME: "test"})
    provider = SdkTracerProvider(resource=resource)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


# ---------------------------------------------------------------------------
# configure_tracing / build_provider
# ---------------------------------------------------------------------------


class TestConfigureTracing:
    def test_returns_sdk_tracer_provider(self):
        from src.observability.tracing import configure_tracing

        provider = configure_tracing("noop")
        assert isinstance(provider, SdkTracerProvider)

    def test_noop_exporter_accepted(self):
        from src.observability.tracing import configure_tracing

        # Should not raise.
        configure_tracing("noop")

    def test_otlp_exporter_returns_provider(self, monkeypatch):
        """OTLP exporter builds successfully (endpoint may be unreachable at test time)."""
        from src.observability.tracing import configure_tracing

        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
        provider = configure_tracing("otlp")
        assert isinstance(provider, SdkTracerProvider)

    def test_jaeger_exporter_returns_provider(self, monkeypatch):
        """Jaeger exporter routes through OTLP (no separate package needed)."""
        from src.observability.tracing import configure_tracing

        monkeypatch.setenv("OTEL_EXPORTER_JAEGER_ENDPOINT", "http://localhost:4317")
        provider = configure_tracing("jaeger")
        assert isinstance(provider, SdkTracerProvider)

    def test_unknown_exporter_falls_back_to_noop(self):
        from src.observability.tracing import configure_tracing

        # Should not raise.
        provider = configure_tracing("datadog")
        assert isinstance(provider, SdkTracerProvider)

    def test_case_insensitive_noop(self):
        from src.observability.tracing import configure_tracing

        provider = configure_tracing("NOOP")
        assert isinstance(provider, SdkTracerProvider)

    def test_case_insensitive_otlp(self, monkeypatch):
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
        from src.observability.tracing import configure_tracing

        provider = configure_tracing("OTLP")
        assert isinstance(provider, SdkTracerProvider)


class TestBuildProvider:
    def test_returns_sdk_tracer_provider(self):
        from src.observability.tracing import build_provider

        provider = build_provider("noop")
        assert isinstance(provider, SdkTracerProvider)

    def test_otlp_build_does_not_touch_global(self, monkeypatch):
        """build_provider must not change the global OTel provider."""
        from opentelemetry import trace as otel_trace  # noqa: PLC0415
        from src.observability.tracing import build_provider  # noqa: PLC0415

        before = otel_trace.get_tracer_provider()
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
        build_provider("otlp")
        after = otel_trace.get_tracer_provider()
        assert before is after

    def test_jaeger_build(self, monkeypatch):
        from src.observability.tracing import build_provider

        monkeypatch.setenv("OTEL_EXPORTER_JAEGER_ENDPOINT", "http://localhost:4317")
        provider = build_provider("jaeger")
        assert isinstance(provider, SdkTracerProvider)

    def test_unknown_exporter_falls_back(self):
        from src.observability.tracing import build_provider

        provider = build_provider("zipkin")
        assert isinstance(provider, SdkTracerProvider)


# ---------------------------------------------------------------------------
# _NoopSpanExporter
# ---------------------------------------------------------------------------


class TestNoopSpanExporter:
    def test_export_returns_success(self):
        from src.observability.tracing import _NoopSpanExporter
        from opentelemetry.sdk.trace.export import SpanExportResult

        exporter = _NoopSpanExporter()
        result = exporter.export([])  # empty list of spans
        assert result == SpanExportResult.SUCCESS

    def test_shutdown_does_not_raise(self):
        from src.observability.tracing import _NoopSpanExporter

        exporter = _NoopSpanExporter()
        exporter.shutdown()  # must not raise


# ---------------------------------------------------------------------------
# trace_provider_call
# ---------------------------------------------------------------------------


class TestTraceProviderCall:
    def setup_method(self):
        self.provider, self.exporter = _isolated_provider()

    def _spans(self):
        return self.exporter.get_finished_spans()

    def test_span_is_emitted(self):
        from src.observability.tracing import trace_provider_call

        with trace_provider_call("angel_one", "fetch_ohlcv", tracer_provider=self.provider):
            pass

        assert len(self._spans()) == 1

    def test_span_name(self):
        from src.observability.tracing import trace_provider_call

        with trace_provider_call("angel_one", "fetch_ohlcv", tracer_provider=self.provider):
            pass

        assert self._spans()[0].name == "provider.angel_one.fetch_ohlcv"

    def test_mds_provider_attribute(self):
        from src.observability.tracing import trace_provider_call

        with trace_provider_call("upstox", "historical", tracer_provider=self.provider):
            pass

        assert self._spans()[0].attributes["mds.provider"] == "upstox"

    def test_mds_instrument_id_attribute(self):
        from src.observability.tracing import trace_provider_call

        with trace_provider_call(
            "angel_one",
            "live_quote",
            instrument_id="NSE:NIFTY:IDX",
            tracer_provider=self.provider,
        ):
            pass

        assert self._spans()[0].attributes["mds.instrumentId"] == "NSE:NIFTY:IDX"

    def test_mds_step_is_provider_call(self):
        from src.observability.tracing import trace_provider_call

        with trace_provider_call("jugaad", "eod", tracer_provider=self.provider):
            pass

        assert self._spans()[0].attributes["mds.step"] == "provider_call"

    def test_mds_cache_hit_is_false(self):
        from src.observability.tracing import trace_provider_call

        with trace_provider_call("openchart", "ohlcv", tracer_provider=self.provider):
            pass

        assert self._spans()[0].attributes["mds.cacheHit"] is False

    def test_mds_duration_ms_is_set(self):
        from src.observability.tracing import trace_provider_call

        with trace_provider_call("binance", "klines", tracer_provider=self.provider):
            time.sleep(0.005)

        span = self._spans()[0]
        assert "mds.durationMs" in span.attributes
        assert span.attributes["mds.durationMs"] >= 0.0

    def test_span_status_ok_on_success(self):
        from src.observability.tracing import trace_provider_call

        with trace_provider_call("deribit", "book_summary", tracer_provider=self.provider):
            pass

        assert self._spans()[0].status.status_code == StatusCode.OK

    def test_exception_sets_error_status_and_reraises(self):
        from src.observability.tracing import trace_provider_call

        with pytest.raises(ValueError, match="provider down"):
            with trace_provider_call(
                "angel_one", "fetch_ohlcv", tracer_provider=self.provider
            ):
                raise ValueError("provider down")

        assert self._spans()[0].status.status_code == StatusCode.ERROR

    def test_no_instrument_id_omits_attribute(self):
        from src.observability.tracing import trace_provider_call

        with trace_provider_call("scrapling", "option_chain", tracer_provider=self.provider):
            pass

        span = self._spans()[0]
        assert "mds.instrumentId" not in span.attributes

    def test_span_yielded_to_caller(self):
        from src.observability.tracing import trace_provider_call
        from opentelemetry.trace import Span  # noqa: PLC0415

        with trace_provider_call(
            "angel_one", "live", tracer_provider=self.provider
        ) as span:
            assert isinstance(span, Span)

    def test_duration_ms_non_negative_on_exception(self):
        """durationMs must be set even when an exception is raised."""
        from src.observability.tracing import trace_provider_call

        with pytest.raises(RuntimeError):
            with trace_provider_call("upstox", "ohlcv", tracer_provider=self.provider):
                raise RuntimeError("boom")

        span = self._spans()[0]
        assert span.attributes.get("mds.durationMs", -1) >= 0.0


# ---------------------------------------------------------------------------
# trace_cache_lookup
# ---------------------------------------------------------------------------


class TestTraceCacheLookup:
    def setup_method(self):
        self.provider, self.exporter = _isolated_provider()

    def _spans(self):
        return self.exporter.get_finished_spans()

    def test_span_name_l1(self):
        from src.observability.tracing import trace_cache_lookup

        with trace_cache_lookup("l1", "get", tracer_provider=self.provider):
            pass

        assert self._spans()[0].name == "cache.l1.get"

    def test_span_name_l2(self):
        from src.observability.tracing import trace_cache_lookup

        with trace_cache_lookup("l2", "set", tracer_provider=self.provider):
            pass

        assert self._spans()[0].name == "cache.l2.set"

    def test_mds_step_is_cache_level(self):
        from src.observability.tracing import trace_cache_lookup

        with trace_cache_lookup("l3", "get", tracer_provider=self.provider):
            pass

        assert self._spans()[0].attributes["mds.step"] == "cache_l3"

    def test_cache_hit_true(self):
        from src.observability.tracing import trace_cache_lookup

        with trace_cache_lookup("l1", "get", cache_hit=True, tracer_provider=self.provider):
            pass

        assert self._spans()[0].attributes["mds.cacheHit"] is True

    def test_cache_hit_false_default(self):
        from src.observability.tracing import trace_cache_lookup

        with trace_cache_lookup("l2", "get", tracer_provider=self.provider):
            pass

        assert self._spans()[0].attributes["mds.cacheHit"] is False

    def test_mds_provider_attribute(self):
        from src.observability.tracing import trace_cache_lookup

        with trace_cache_lookup("l2", "get", provider="angel_one", tracer_provider=self.provider):
            pass

        assert self._spans()[0].attributes["mds.provider"] == "angel_one"

    def test_mds_instrument_id_attribute(self):
        from src.observability.tracing import trace_cache_lookup

        with trace_cache_lookup(
            "l1", "get", instrument_id="NSE:RELIANCE:EQ", tracer_provider=self.provider
        ):
            pass

        assert self._spans()[0].attributes["mds.instrumentId"] == "NSE:RELIANCE:EQ"

    def test_duration_ms_present(self):
        from src.observability.tracing import trace_cache_lookup

        with trace_cache_lookup("l2", "get", tracer_provider=self.provider):
            pass

        assert "mds.durationMs" in self._spans()[0].attributes

    def test_exception_sets_error_status(self):
        from src.observability.tracing import trace_cache_lookup

        with pytest.raises(RuntimeError):
            with trace_cache_lookup("l2", "get", tracer_provider=self.provider):
                raise RuntimeError("redis down")

        assert self._spans()[0].status.status_code == StatusCode.ERROR

    def test_span_status_ok_on_success(self):
        from src.observability.tracing import trace_cache_lookup

        with trace_cache_lookup("l1", "get", cache_hit=True, tracer_provider=self.provider):
            pass

        assert self._spans()[0].status.status_code == StatusCode.OK

    def test_no_provider_omits_attribute(self):
        from src.observability.tracing import trace_cache_lookup

        with trace_cache_lookup("l1", "get", tracer_provider=self.provider):
            pass

        assert "mds.provider" not in self._spans()[0].attributes


# ---------------------------------------------------------------------------
# trace_pipeline_step
# ---------------------------------------------------------------------------


class TestTracePipelineStep:
    def setup_method(self):
        self.provider, self.exporter = _isolated_provider()

    def _spans(self):
        return self.exporter.get_finished_spans()

    def test_span_name(self):
        from src.observability.tracing import trace_pipeline_step

        with trace_pipeline_step("normalise", tracer_provider=self.provider):
            pass

        assert self._spans()[0].name == "pipeline.normalise"

    def test_mds_step_attribute(self):
        from src.observability.tracing import trace_pipeline_step

        with trace_pipeline_step("timestamp_normalise", tracer_provider=self.provider):
            pass

        assert self._spans()[0].attributes["mds.step"] == "timestamp_normalise"

    def test_mds_cache_hit_false(self):
        from src.observability.tracing import trace_pipeline_step

        with trace_pipeline_step("dedup", tracer_provider=self.provider):
            pass

        assert self._spans()[0].attributes["mds.cacheHit"] is False

    def test_mds_provider_optional(self):
        from src.observability.tracing import trace_pipeline_step

        with trace_pipeline_step(
            "quality_scoring", provider="angel_one", tracer_provider=self.provider
        ):
            pass

        assert self._spans()[0].attributes["mds.provider"] == "angel_one"

    def test_mds_instrument_id_optional(self):
        from src.observability.tracing import trace_pipeline_step

        with trace_pipeline_step(
            "gap_detection", instrument_id="NSE:NIFTY:IDX", tracer_provider=self.provider
        ):
            pass

        assert self._spans()[0].attributes["mds.instrumentId"] == "NSE:NIFTY:IDX"

    def test_duration_ms_non_negative(self):
        from src.observability.tracing import trace_pipeline_step

        with trace_pipeline_step("schema_validate", tracer_provider=self.provider):
            pass

        assert self._spans()[0].attributes["mds.durationMs"] >= 0.0

    def test_exception_propagates_and_sets_error_status(self):
        from src.observability.tracing import trace_pipeline_step

        with pytest.raises(ValueError, match="invariant"):
            with trace_pipeline_step("ohlcv_validate", tracer_provider=self.provider):
                raise ValueError("invariant violated")

        assert self._spans()[0].status.status_code == StatusCode.ERROR

    def test_no_optional_args_does_not_raise(self):
        from src.observability.tracing import trace_pipeline_step

        with trace_pipeline_step("raw_receipt", tracer_provider=self.provider):
            pass

        span = self._spans()[0]
        assert "mds.provider" not in span.attributes
        assert "mds.instrumentId" not in span.attributes

    def test_all_14_pipeline_steps_produce_spans(self):
        """Each of the 14 pipeline steps should produce a correctly-named span."""
        from src.observability.tracing import trace_pipeline_step

        steps = [
            "raw_receipt",
            "schema_validate",
            "normalise",
            "timestamp_normalise",
            "semantic_validate",
            "dedup",
            "gap_detect",
            "freshness_classify",
            "reconcile",
            "quality_score",
            "canonical_output",
            "cache_populate",
            "persist",
            "deliver",
        ]

        for step in steps:
            with trace_pipeline_step(step, tracer_provider=self.provider):
                pass

        finished = self.exporter.get_finished_spans()
        assert len(finished) == 14
        emitted_names = {s.name for s in finished}
        for step in steps:
            assert f"pipeline.{step}" in emitted_names


# ---------------------------------------------------------------------------
# shutdown_tracing
# ---------------------------------------------------------------------------


class TestShutdownTracing:
    def test_shutdown_is_safe_when_never_configured(self):
        """Calling shutdown when _tracer_provider is None must not raise."""
        import src.observability.tracing as mod  # noqa: PLC0415

        original = mod._tracer_provider
        mod._tracer_provider = None
        try:
            mod.shutdown_tracing()  # must not raise
        finally:
            mod._tracer_provider = original

    def test_shutdown_clears_provider(self):
        """After shutdown, _tracer_provider is None."""
        import src.observability.tracing as mod  # noqa: PLC0415
        from src.observability.tracing import build_provider  # noqa: PLC0415

        # Plant a real (isolated) provider so we can shut it down.
        provider = build_provider("noop")
        mod._tracer_provider = provider
        mod.shutdown_tracing()
        assert mod._tracer_provider is None

    def test_double_shutdown_is_idempotent(self):
        from src.observability.tracing import shutdown_tracing

        shutdown_tracing()
        shutdown_tracing()  # second call must not raise


# ---------------------------------------------------------------------------
# get_tracer
# ---------------------------------------------------------------------------


class TestGetTracer:
    def test_returns_tracer_with_explicit_provider(self):
        from src.observability.tracing import get_tracer

        provider, _ = _isolated_provider()
        tracer = get_tracer(tracer_provider=provider)
        assert tracer is not None

    def test_returns_tracer_with_custom_name(self):
        from src.observability.tracing import get_tracer

        provider, _ = _isolated_provider()
        tracer = get_tracer("my_component", tracer_provider=provider)
        assert tracer is not None

    def test_returns_tracer_without_provider(self):
        """Falls back to the global provider — must not raise."""
        from src.observability.tracing import get_tracer

        tracer = get_tracer()
        assert tracer is not None
