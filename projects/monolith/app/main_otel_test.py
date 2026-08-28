"""Coverage for app.main -- OpenTelemetry instrumentation and shutdown.

Uses importlib.reload() instead of pop-and-reimport because app.main transitively
imports C extension modules (numpy via pydantic_ai) that cannot be loaded twice.
"""

import importlib
import os
import sys
from unittest.mock import MagicMock, patch

# Ensure no valid STATIC_DIR is set so the StaticFiles mount is skipped.
os.environ.pop("STATIC_DIR", None)

# Force an initial import so reload() has something to work with.
import app.main  # noqa: F401, E402

_OTEL_ENDPOINT = "http://collector.example:4318/v1/traces"


def _make_otel_fake_modules():
    """Build fake sys.modules entries for all opentelemetry subpackages used by main.py."""
    mock_instrumentor_class = MagicMock()
    mock_instrumentor_class.instrument_app = MagicMock()

    mock_fastapi_module = MagicMock()
    mock_fastapi_module.FastAPIInstrumentor = mock_instrumentor_class

    return {
        "opentelemetry": MagicMock(),
        "opentelemetry.exporter": MagicMock(),
        "opentelemetry.exporter.otlp": MagicMock(),
        "opentelemetry.exporter.otlp.proto": MagicMock(),
        "opentelemetry.exporter.otlp.proto.http": MagicMock(),
        "opentelemetry.exporter.otlp.proto.http.trace_exporter": MagicMock(),
        "opentelemetry.instrumentation": MagicMock(),
        "opentelemetry.instrumentation.fastapi": mock_fastapi_module,
        "opentelemetry.instrumentation.httpx": MagicMock(),
        "opentelemetry.sdk": MagicMock(),
        "opentelemetry.sdk.resources": MagicMock(),
        "opentelemetry.sdk.trace": MagicMock(),
        "opentelemetry.sdk.trace.export": MagicMock(),
    }, mock_instrumentor_class


class TestOtelInstrumentation:
    def test_instrument_app_called(self):
        """FastAPIInstrumentor.instrument_app() is called on module load."""
        fake_modules, mock_instrumentor_class = _make_otel_fake_modules()

        with (
            patch.dict(sys.modules, fake_modules),
            patch.dict(
                os.environ,
                {"OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": _OTEL_ENDPOINT},
            ),
        ):
            importlib.reload(app.main)

        mock_instrumentor_class.instrument_app.assert_called_once()

    def test_tracer_provider_is_set(self):
        """_tracer_provider is set to a non-None value after module load."""
        fake_modules, _ = _make_otel_fake_modules()

        with (
            patch.dict(sys.modules, fake_modules),
            patch.dict(
                os.environ,
                {"OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": _OTEL_ENDPOINT},
            ),
        ):
            importlib.reload(app.main)

        assert app.main._tracer_provider is not None
