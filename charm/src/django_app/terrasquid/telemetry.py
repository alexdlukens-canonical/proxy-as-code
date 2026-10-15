"""Optional workload tracing and log correlation."""

import logging
import os

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.django import DjangoInstrumentor
from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

_provider: TracerProvider | None = None


def configure_tracing() -> TracerProvider | None:
    """Initialize tracing inside the worker or management-command process."""
    global _provider
    if _provider is not None or not os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"):
        return _provider

    _provider = TracerProvider(resource=Resource.create({}))
    _provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(_provider)
    DjangoInstrumentor().instrument(excluded_urls=r".*/metrics/?(?:\?.*)?$,.*/api/v1/status/?(?:\?.*)?$")
    PsycopgInstrumentor().instrument()
    return _provider


class TraceContextFilter(logging.Filter):
    """Attach active trace identifiers without recording request headers or bodies."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Add correlation identifiers, using zeroes outside a trace."""
        context = trace.get_current_span().get_span_context()
        record.trace_id = format(context.trace_id, "032x")
        record.span_id = format(context.span_id, "016x")
        return True
