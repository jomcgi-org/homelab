"""Optional tracing lifecycle for one-shot jobs, without serving instrumentation."""

from __future__ import annotations

import logging


def setup(endpoint: str, service_name: str, logger: logging.Logger):
    """Install a provider when enabled; leave endpoint parsing to the caller."""
    if not endpoint:
        return None
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        provider = TracerProvider(
            resource=Resource.create({"service.name": service_name})
        )
        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint))
        )
        trace.set_tracer_provider(provider)
        logger.info("OpenTelemetry instrumentation enabled")
        return provider
    except Exception:
        logger.warning("Failed to initialize OpenTelemetry", exc_info=True)
        return None


def shutdown(provider, logger: logging.Logger) -> None:
    """Flush once and always attempt shutdown, preserving the workload result."""
    if provider is None:
        return
    try:
        if not provider.force_flush():
            logger.warning("OpenTelemetry force_flush timed out")
    except Exception:
        logger.warning("Failed to flush OpenTelemetry", exc_info=True)
    try:
        provider.shutdown()
    except Exception:
        logger.warning("Failed to shut down OpenTelemetry", exc_info=True)
