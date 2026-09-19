"""Tests for core.log -- filters, formatting, and configure_logging()."""

import asyncio
import io
import logging
from unittest.mock import MagicMock, patch

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags, TraceState

from core.log import (
    _PLAIN_FORMAT,
    _HealthzFilter,
    _TraceContextFormatter,
    configure_logging,
)


class TestHealthzFilter:
    def test_suppresses_healthz_records(self):
        """_HealthzFilter.filter() returns False for records containing '/healthz'."""
        f = _HealthzFilter()
        record = MagicMock(spec=logging.LogRecord)
        record.getMessage.return_value = "GET /healthz HTTP/1.1 200"
        assert f.filter(record) is False

    def test_passes_non_healthz_records(self):
        """_HealthzFilter.filter() returns True for records not containing '/healthz'."""
        f = _HealthzFilter()
        record = MagicMock(spec=logging.LogRecord)
        record.getMessage.return_value = "GET /api/todo/daily HTTP/1.1 200"
        assert f.filter(record) is True

    def test_passes_empty_message(self):
        """_HealthzFilter.filter() returns True for empty log messages."""
        f = _HealthzFilter()
        record = MagicMock(spec=logging.LogRecord)
        record.getMessage.return_value = ""
        assert f.filter(record) is True

    def test_suppresses_when_healthz_in_path_only(self):
        """_HealthzFilter.filter() suppresses any message containing '/healthz' substring."""
        f = _HealthzFilter()
        record = MagicMock(spec=logging.LogRecord)
        record.getMessage.return_value = "some prefix /healthz suffix"
        assert f.filter(record) is False

    def test_suppresses_healthz_in_any_context(self):
        """_HealthzFilter.filter() suppresses any message that contains '/healthz'."""
        f = _HealthzFilter()
        record = MagicMock(spec=logging.LogRecord)
        # Note: '/healthz' substring present — should be suppressed
        record.getMessage.return_value = "debug: probing /healthz endpoint"
        assert f.filter(record) is False


class TestConfigureLogging:
    def setup_method(self):
        """Clear uvicorn.access filters before each test to prevent _HealthzFilter accumulation.

        configure_logging() calls addFilter() without deduplication, so repeated calls
        across tests would stack multiple _HealthzFilter instances on the global logger.
        Resetting here keeps each test isolated regardless of execution order.
        """
        logging.getLogger("uvicorn.access").filters.clear()

    def test_sets_root_logger_level(self):
        """configure_logging() sets the root logger to the specified level."""
        configure_logging(logging.DEBUG)
        assert logging.getLogger().level == logging.DEBUG
        # reset
        configure_logging(logging.INFO)

    def test_root_level_defaults_to_info(self):
        """configure_logging() defaults the root logger to INFO."""
        configure_logging()
        assert logging.getLogger().level == logging.INFO

    def test_discord_gateway_set_to_warning(self):
        """configure_logging() sets discord.gateway to WARNING."""
        configure_logging()
        assert logging.getLogger("discord.gateway").level == logging.WARNING

    def test_discord_client_set_to_error(self):
        """configure_logging() sets discord.client to ERROR."""
        configure_logging()
        assert logging.getLogger("discord.client").level == logging.ERROR

    def test_httpx_set_to_warning(self):
        """configure_logging() sets httpx to WARNING."""
        configure_logging()
        assert logging.getLogger("httpx").level == logging.WARNING

    def test_httpcore_set_to_warning(self):
        """configure_logging() sets httpcore to WARNING."""
        configure_logging()
        assert logging.getLogger("httpcore").level == logging.WARNING

    def test_healthz_filter_attached_to_uvicorn_access(self):
        """configure_logging() adds a _HealthzFilter to uvicorn.access logger."""
        configure_logging()
        uvicorn_access = logging.getLogger("uvicorn.access")
        filter_types = [type(f) for f in uvicorn_access.filters]
        assert _HealthzFilter in filter_types, (
            "Expected _HealthzFilter to be attached to uvicorn.access logger"
        )


def _record(message: str = "probe failed") -> logging.LogRecord:
    return logging.LogRecord(
        name="trace-test",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )


def _exception_record() -> logging.LogRecord:
    try:
        raise ValueError("boom")
    except ValueError as error:
        return logging.LogRecord(
            name="trace-test",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="embedding call failed",
            args=(),
            exc_info=(type(error), error, error.__traceback__),
        )


def _configured_line(include_trace_context: bool, emit) -> str:
    stream = io.StringIO()
    try:
        with patch("core.log.sys.stdout", stream):
            configure_logging(include_trace_context=include_trace_context)
            emit()
        return stream.getvalue().strip()
    finally:
        configure_logging()


class TestTraceContextFormatter:
    def setup_method(self):
        self.formatter = _TraceContextFormatter(_PLAIN_FORMAT)
        self.provider = TracerProvider()
        self.tracer = self.provider.get_tracer(__name__)

    def test_no_span_keeps_plain_line_byte_identical(self):
        assert self.formatter.format(_record()) == "WARNING trace-test: probe failed"

    def test_non_recording_span_keeps_plain_line_byte_identical(self):
        context = SpanContext(
            trace_id=0x1234,
            span_id=0x5678,
            is_remote=False,
            trace_flags=TraceFlags(0),
            trace_state=TraceState(),
        )

        with trace.use_span(NonRecordingSpan(context), end_on_exit=False):
            line = self.formatter.format(_record())

        assert line == "WARNING trace-test: probe failed"

    def test_private_format_appends_fixed_width_ids_for_recording_span(self):
        with self.tracer.start_as_current_span("synthetic-probe") as span:
            context = span.get_span_context()
            line = self.formatter.format(
                _record("ember synthetic qwen failed: model unavailable")
            )

        assert line == (
            "WARNING trace-test: ember synthetic qwen failed: model unavailable"
            f" trace_id={context.trace_id:032x} span_id={context.span_id:016x}"
        )

    def test_no_span_exception_keeps_plain_output_byte_identical(self):
        record = _exception_record()

        expected = logging.Formatter(_PLAIN_FORMAT).format(record)

        assert self.formatter.format(record) == expected

    def test_recording_span_ids_precede_unmodified_exception_text(self):
        record = _exception_record()
        plain_lines = logging.Formatter(_PLAIN_FORMAT).format(record).splitlines()

        with self.tracer.start_as_current_span("failed-embedding") as span:
            context = span.get_span_context()
            traced_lines = self.formatter.format(record).splitlines()

        assert traced_lines[0] == (
            "ERROR trace-test: embedding call failed"
            f" trace_id={context.trace_id:032x} span_id={context.span_id:016x}"
        )
        assert traced_lines[1:] == plain_lines[1:]

    def test_public_configuration_is_plain_even_during_recording_span(self):
        with self.tracer.start_as_current_span("public-request"):
            line = _configured_line(
                False, lambda: logging.getLogger("trace-test").warning("public line")
            )

        assert line == "WARNING trace-test: public line"

    def test_private_configuration_uses_emission_context(self):
        with self.tracer.start_as_current_span("private-request") as span:
            context = span.get_span_context()
            line = _configured_line(
                True, lambda: logging.getLogger("trace-test").warning("private line")
            )

        assert line == (
            "WARNING trace-test: private line"
            f" trace_id={context.trace_id:032x} span_id={context.span_id:016x}"
        )

    def test_nested_span_exit_restores_parent_and_then_cleans_up(self):
        with self.tracer.start_as_current_span("outer") as outer:
            outer_context = outer.get_span_context()
            before = self.formatter.format(_record("outer before"))

            with self.tracer.start_as_current_span("inner") as inner:
                inner_context = inner.get_span_context()
                nested = self.formatter.format(_record("inner"))

            restored = self.formatter.format(_record("outer after"))

        after = self.formatter.format(_record("after"))
        assert f"span_id={outer_context.span_id:016x}" in before
        assert f"span_id={inner_context.span_id:016x}" in nested
        assert f"span_id={outer_context.span_id:016x}" in restored
        assert inner_context.span_id != outer_context.span_id
        assert after == "WARNING trace-test: after"

    def test_concurrent_tasks_do_not_cross_contaminate_ids(self):
        async def emit(name: str):
            with self.tracer.start_as_current_span(name) as span:
                context = span.get_span_context()
                await asyncio.sleep(0)
                return context, self.formatter.format(_record(name))

        async def run_both():
            return await asyncio.gather(emit("first"), emit("second"))

        first, second = asyncio.run(run_both())
        first_context, first_line = first
        second_context, second_line = second

        assert first_context.trace_id != second_context.trace_id
        assert f"trace_id={first_context.trace_id:032x}" in first_line
        assert f"trace_id={second_context.trace_id:032x}" in second_line
        assert f"trace_id={second_context.trace_id:032x}" not in first_line
        assert f"trace_id={first_context.trace_id:032x}" not in second_line
