"""Shared structured logging configuration for the monolith backend."""

import logging
import sys

from opentelemetry import trace

_PLAIN_FORMAT = "%(levelname)s %(name)s: %(message)s"


class _TraceContextFormatter(logging.Formatter):
    """Append active recording span IDs to the primary log message line."""

    def formatMessage(self, record: logging.LogRecord) -> str:
        message = super().formatMessage(record)
        try:
            span = trace.get_current_span()
            if not span.is_recording():
                return message
            context = span.get_span_context()
            if not context.is_valid:
                return message
            return (
                f"{message} trace_id={context.trace_id:032x}"
                f" span_id={context.span_id:016x}"
            )
        except (AttributeError, RuntimeError, TypeError, ValueError):
            # Logging must remain available even if tracing is misconfigured.
            return message


class _HealthzFilter(logging.Filter):
    """Suppress Uvicorn access log entries for health check probes."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return "/healthz" not in msg


def configure_logging(
    level: int = logging.INFO, *, include_trace_context: bool = False
) -> None:
    """Configure the root logger with a structured format.

    Call once at startup (before any getLogger calls emit) so every
    module that uses ``logging.getLogger(__name__)`` inherits a
    handler and level automatically.
    """
    logging.basicConfig(
        level=level,
        stream=sys.stdout,
        format=_PLAIN_FORMAT,
        force=True,
    )
    if include_trace_context:
        # basicConfig has installed the root handlers. Replacing only their
        # formatter keeps its stream/level behavior while making correlation a
        # private-profile choice. The formatter reads the ContextVar-backed OTel
        # context at emission, so nested and concurrent requests cannot leak IDs.
        for handler in logging.getLogger().handlers:
            handler.setFormatter(_TraceContextFormatter(_PLAIN_FORMAT))
    # Quiet noisy libraries
    logging.getLogger("discord.gateway").setLevel(logging.WARNING)
    logging.getLogger("discord.client").setLevel(logging.ERROR)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("uvicorn").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.error").setLevel(logging.WARNING)
    # Suppress healthcheck probe noise
    logging.getLogger("uvicorn.access").addFilter(_HealthzFilter())
