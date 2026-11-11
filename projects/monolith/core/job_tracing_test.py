"""Shared one-shot tracing lifecycle tests; all exporters and providers are fakes."""

import builtins
import logging
import runpy
from pathlib import Path
from unittest import mock

import pytest

from core import job_tracing


@pytest.fixture
def logger():
    return mock.Mock(spec=logging.Logger)


def test_import_and_disabled_setup_never_import_otel(monkeypatch, logger):
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        assert not name.startswith("opentelemetry"), name
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    namespace = runpy.run_path(str(Path(job_tracing.__file__)))
    assert namespace["setup"]("", "test-job", logger) is None
    logger.info.assert_not_called()


def test_setup_uses_explicit_endpoint_identity_and_logger(logger):
    with (
        mock.patch("opentelemetry.sdk.trace.TracerProvider") as provider_class,
        mock.patch("opentelemetry.sdk.resources.Resource.create") as resource,
        mock.patch("opentelemetry.sdk.trace.export.BatchSpanProcessor") as processor,
        mock.patch(
            "opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter"
        ) as exporter,
        mock.patch("opentelemetry.trace.set_tracer_provider") as register,
    ):
        provider = job_tracing.setup(" endpoint ", "test-job", logger)

    assert provider is provider_class.return_value
    resource.assert_called_once_with({"service.name": "test-job"})
    provider_class.assert_called_once_with(resource=resource.return_value)
    exporter.assert_called_once_with(endpoint=" endpoint ")
    processor.assert_called_once_with(exporter.return_value)
    provider.add_span_processor.assert_called_once_with(processor.return_value)
    register.assert_called_once_with(provider)
    logger.info.assert_called_once_with("OpenTelemetry instrumentation enabled")


@pytest.mark.parametrize(
    "failure",
    [
        "opentelemetry.sdk.resources.Resource.create",
        "opentelemetry.sdk.trace.TracerProvider",
        "opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter",
        "opentelemetry.sdk.trace.export.BatchSpanProcessor",
        "opentelemetry.trace.set_tracer_provider",
    ],
)
def test_setup_failure_is_non_fatal(failure, logger):
    with (
        mock.patch("opentelemetry.sdk.trace.TracerProvider"),
        mock.patch(
            "opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter"
        ),
        mock.patch("opentelemetry.sdk.trace.export.BatchSpanProcessor"),
        mock.patch("opentelemetry.trace.set_tracer_provider"),
        mock.patch(failure, side_effect=RuntimeError("telemetry unavailable")),
    ):
        assert job_tracing.setup("endpoint", "test-job", logger) is None
    logger.warning.assert_called_once_with(
        "Failed to initialize OpenTelemetry", exc_info=True
    )


def test_import_failure_is_non_fatal(monkeypatch, logger):
    original_import = builtins.__import__

    def missing_otel(name, *args, **kwargs):
        if name.startswith("opentelemetry"):
            raise ImportError("no OTel installed")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing_otel)
    assert job_tracing.setup("endpoint", "test-job", logger) is None
    logger.warning.assert_called_once_with(
        "Failed to initialize OpenTelemetry", exc_info=True
    )


def test_shutdown_disabled(logger):
    job_tracing.shutdown(None, logger)
    logger.warning.assert_not_called()


@pytest.mark.parametrize("flush", [True, False, RuntimeError("flush failed")])
@pytest.mark.parametrize("shutdown_error", [None, RuntimeError("shutdown failed")])
def test_shutdown_always_runs_once_after_flush(flush, shutdown_error, logger):
    provider = mock.Mock()
    if isinstance(flush, Exception):
        provider.force_flush.side_effect = flush
    else:
        provider.force_flush.return_value = flush
    provider.shutdown.side_effect = shutdown_error

    job_tracing.shutdown(provider, logger)

    assert provider.mock_calls == [mock.call.force_flush(), mock.call.shutdown()]
    expected = []
    if flush is False:
        expected.append(mock.call("OpenTelemetry force_flush timed out"))
    elif isinstance(flush, Exception):
        expected.append(mock.call("Failed to flush OpenTelemetry", exc_info=True))
    if shutdown_error:
        expected.append(mock.call("Failed to shut down OpenTelemetry", exc_info=True))
    assert logger.warning.call_args_list == expected


def test_cleanup_preserves_workload_exception(logger):
    provider = mock.Mock()
    provider.force_flush.side_effect = RuntimeError("flush failed")
    provider.shutdown.side_effect = RuntimeError("shutdown failed")
    failure = ValueError("workload failed")
    with pytest.raises(ValueError) as raised:
        try:
            raise failure
        finally:
            job_tracing.shutdown(provider, logger)
    assert raised.value is failure
    assert provider.mock_calls == [mock.call.force_flush(), mock.call.shutdown()]
