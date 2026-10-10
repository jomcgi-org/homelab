import builtins
from datetime import datetime, timezone

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import StatusCode

import ember_public.synthetic_probe as probe
from ember_public.synthetic_models import EmberSyntheticProbe


@pytest.fixture
def exported_spans(monkeypatch):
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(probe, "_tracer", provider.get_tracer("ember.synthetic_probe"))
    yield exporter
    provider.shutdown()


def test_span_detail_bound_matches_trigger_filter():
    # The Honeycomb ember-demo-probe-failed trigger filters on the bounded
    # ember.probe.detail attribute; the bound is asserted so it cannot drift.
    assert probe._SPAN_DETAIL_MAX_CHARS == 512


def test_current_trace_id_is_none_for_invalid_context(monkeypatch):
    class SpanContext:
        is_valid = False
        trace_id = 0

    class Span:
        def get_span_context(self):
            return SpanContext()

    monkeypatch.setattr(probe.trace, "get_current_span", lambda: Span())

    assert probe._current_trace_id() is None


@pytest.mark.asyncio
async def test_session_probes_get_one_independent_root_per_logical_run(
    monkeypatch, exported_spans
):
    """Each probe run is its own trace root, even inside a request span."""
    seen = []

    class Turn:
        terminal_reason = "completed"
        result = "ok"

    async def run_session(*_, **__):
        seen.append(probe._current_trace_id())
        return Turn()

    monkeypatch.setattr("factory.execution.api.run_synthetic_session", run_session)

    with probe._tracer.start_as_current_span("request") as request:
        first = await probe.probe_codex()
        second = await probe.probe_spark()
        assert (
            probe._current_trace_id() == f"{request.get_span_context().trace_id:032x}"
        )

    roots = [s for s in exported_spans.get_finished_spans() if s.parent is None]
    assert seen == [first["trace_id"], second["trace_id"]]
    assert first["trace_id"] != second["trace_id"]
    assert [s.name for s in roots] == [
        "ember.probe.codex",
        "ember.probe.spark",
        "request",
    ]
    assert first["trace_id"] == f"{roots[0].context.trace_id:032x}"
    assert second["trace_id"] == f"{roots[1].context.trace_id:032x}"
    assert len({s.context.trace_id for s in roots}) == 3


@pytest.mark.asyncio
async def test_probe_codex_success(monkeypatch, exported_spans):
    class Turn:
        terminal_reason = "completed"
        result = " codex synthetic ok "

    async def run_session(prompt, model, on_ember_session_id):
        assert prompt == "Reply with exactly: codex synthetic ok"
        assert model == "luna"
        on_ember_session_id("ember-codex")
        return Turn()

    monkeypatch.setattr("factory.execution.api.run_synthetic_session", run_session)

    result = await probe.probe_codex()

    assert result["ok"] is True
    assert isinstance(result["latency_ms"], (int, float))
    (root,) = exported_spans.get_finished_spans()
    assert result["trace_id"] == f"{root.context.trace_id:032x}"
    assert result["ember_session_id"] == "ember-codex"
    assert root.name == "ember.probe.codex"
    assert root.parent is None
    assert root.attributes["ember.probe.demo"] == "codex"
    assert root.attributes["ember.probe.ok"] is True
    assert root.status.status_code is not StatusCode.ERROR


@pytest.mark.asyncio
async def test_probe_codex_handles_none_from_run_synthetic_session(monkeypatch):
    async def run_session(*_, **__):
        return None

    monkeypatch.setattr("factory.execution.api.run_synthetic_session", run_session)

    result = await probe.probe_codex()

    assert result["ok"] is True
    assert result["detail"] == "another replica delivered this run"


@pytest.mark.asyncio
async def test_probe_codex_bad_terminal_reason(monkeypatch):
    class Turn:
        terminal_reason = "pending"
        result = "not done"

    async def run_session(*_, **__):
        return Turn()

    monkeypatch.setattr("factory.execution.api.run_synthetic_session", run_session)

    result = await probe.probe_codex()

    assert result["ok"] is False
    assert "turn reason" in result["detail"]


@pytest.mark.asyncio
async def test_probe_codex_empty_result(monkeypatch):
    class Turn:
        terminal_reason = "completed"
        result = ""

    async def run_session(*_, **__):
        return Turn()

    monkeypatch.setattr("factory.execution.api.run_synthetic_session", run_session)

    result = await probe.probe_codex()

    assert result["ok"] is False
    assert "empty result" in result["detail"]


@pytest.mark.asyncio
async def test_probe_codex_exception(monkeypatch):
    async def run_session(*_, on_ember_session_id, **__):
        on_ember_session_id("ember-failed")
        raise RuntimeError("Codex transport unavailable")

    monkeypatch.setattr("factory.execution.api.run_synthetic_session", run_session)

    result = await probe.probe_codex()

    assert result["ok"] is False
    assert result["latency_ms"] is None
    assert result["ember_session_id"] == "ember-failed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("run", "demo"), [(probe.probe_codex, "codex"), (probe.probe_spark, "spark")]
)
@pytest.mark.parametrize("failure", ["reason", "empty", "exception"])
async def test_failed_session_probe_root_matches_trigger_filter(
    monkeypatch, exported_spans, run, demo, failure
):
    class Turn:
        terminal_reason = "pending" if failure == "reason" else "completed"
        result = "" if failure == "empty" else "not done"

    async def run_session(*_, **__):
        if failure == "exception":
            raise RuntimeError("x" * 1000)
        return Turn()

    monkeypatch.setattr("factory.execution.api.run_synthetic_session", run_session)

    result = await run()

    assert result["ok"] is False
    (root,) = exported_spans.get_finished_spans()
    assert root.name == f"ember.probe.{demo}"
    assert root.parent is None
    assert root.attributes["ember.probe.demo"] == demo
    assert root.attributes["ember.probe.ok"] is False
    assert root.attributes["ember.probe.detail"] == result["detail"][:512]
    assert len(root.attributes["ember.probe.detail"]) <= 512
    assert root.status.status_code is StatusCode.ERROR
    assert result["trace_id"] == f"{root.context.trace_id:032x}"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("session_probe", "blocked_module"),
    [
        (probe.probe_codex, "factory.execution.constants"),
        (probe.probe_spark, "factory.execution.api"),
    ],
)
async def test_probe_session_import_failure_is_reported(
    monkeypatch, session_probe, blocked_module
):
    real_import = builtins.__import__

    def fail_selected_import(name, *args, **kwargs):
        if name == blocked_module:
            raise ImportError(f"cannot import {blocked_module}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fail_selected_import)

    result = await session_probe()

    assert result["ok"] is False
    assert result["latency_ms"] is None
    assert result["detail"] == f"cannot import {blocked_module}"


@pytest.mark.asyncio
async def test_probe_spark_success(monkeypatch, exported_spans):
    class Turn:
        terminal_reason = "completed"
        result = " spark synthetic ok "

    async def run_session(prompt, model, on_ember_session_id):
        assert prompt == "Reply with exactly: spark synthetic ok"
        assert model == "spark"
        on_ember_session_id("ember-spark")
        return Turn()

    monkeypatch.setattr("factory.execution.api.run_synthetic_session", run_session)

    result = await probe.probe_spark()

    assert result["ok"] is True
    assert isinstance(result["latency_ms"], (int, float))
    (root,) = exported_spans.get_finished_spans()
    assert result["trace_id"] == f"{root.context.trace_id:032x}"
    assert result["ember_session_id"] == "ember-spark"
    assert root.name == "ember.probe.spark"
    assert root.parent is None
    assert root.attributes["ember.probe.demo"] == "spark"
    assert root.attributes["ember.probe.ok"] is True
    assert root.status.status_code is not StatusCode.ERROR


@pytest.mark.asyncio
async def test_probe_spark_bad_terminal_reason(monkeypatch):
    class Turn:
        terminal_reason = "pending"
        result = "not done"

    async def run_session(*_, **__):
        return Turn()

    monkeypatch.setattr("factory.execution.api.run_synthetic_session", run_session)

    result = await probe.probe_spark()

    assert result["ok"] is False
    assert "turn reason" in result["detail"]


@pytest.mark.asyncio
async def test_probe_spark_empty_result(monkeypatch):
    class Turn:
        terminal_reason = "completed"
        result = ""

    async def run_session(*_, **__):
        return Turn()

    monkeypatch.setattr("factory.execution.api.run_synthetic_session", run_session)

    result = await probe.probe_spark()

    assert result["ok"] is False
    assert "empty result" in result["detail"]


@pytest.mark.asyncio
async def test_probe_spark_exception(monkeypatch):
    async def run_session(*_, **__):
        raise RuntimeError("Spark transport unavailable")

    monkeypatch.setattr("factory.execution.api.run_synthetic_session", run_session)

    result = await probe.probe_spark()

    assert result["ok"] is False
    assert result["latency_ms"] is None


@pytest.mark.asyncio
async def test_record_preserves_last_ok_at_on_failure(monkeypatch):
    class FakeSession:
        row = EmberSyntheticProbe(
            demo="codex",
            ok=True,
            detail="old",
            trace_id="a" * 32,
            ember_session_id="ember-old",
            checked_at=datetime.now(timezone.utc),
            last_ok_at=datetime(2026, 7, 27, tzinfo=timezone.utc),
        )

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def get(self, *_):
            return self.row

        def commit(self):
            return None

    session = FakeSession()
    monkeypatch.setattr(probe, "Session", lambda *_: session)
    old = session.row.last_ok_at
    await probe.record("codex", {"ok": False, "detail": "failed", "latency_ms": None})
    assert session.row.ok is False
    assert session.row.last_ok_at == old
    assert session.row.trace_id is None
    assert session.row.ember_session_id is None


@pytest.mark.asyncio
async def test_record_skips_a_skipped_result(monkeypatch):
    def never(*_):
        pytest.fail("a skipped result must not open a session")

    monkeypatch.setattr(probe, "Session", never)
    await probe.record("codex", {"ok": True, "detail": "busy", "skip": True})
