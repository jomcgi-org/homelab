import builtins
import runpy
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


@pytest.fixture(autouse=True)
def no_retry_wait_in_existing_tests(monkeypatch):
    monkeypatch.setattr(probe, "EMBER_SYNTHETIC_RETRY_BUDGET_S", 0.0)


@pytest.mark.asyncio
async def test_retry_recovers_and_reports_retry(monkeypatch):
    results = iter(
        [
            {"ok": False, "detail": "control plane unavailable", "latency_ms": None},
            {"ok": True, "detail": "warm, 501ms", "latency_ms": 501},
        ]
    )
    monkeypatch.setattr(probe, "EMBER_SYNTHETIC_RETRY_BUDGET_S", 30.0)
    monkeypatch.setattr(probe.asyncio, "sleep", lambda *_: _done())
    monkeypatch.setattr(probe, "perf_counter", _clock([0.0, 0.0, 15.0]))
    monkeypatch.setattr(probe, "_probe_bazel_once", lambda: _next_result(results))

    result = await probe.probe_bazel()

    assert result["ok"] is True
    assert result["detail"] == "warm, 501ms (recovered after 1 retries)"


@pytest.mark.asyncio
async def test_retry_returns_last_failure_detail(monkeypatch):
    results = iter(
        [
            {"ok": False, "detail": "first failure", "latency_ms": None},
            {"ok": False, "detail": "last failure", "latency_ms": None},
        ]
    )
    monkeypatch.setattr(probe, "EMBER_SYNTHETIC_RETRY_BUDGET_S", 30.0)
    monkeypatch.setattr(probe.asyncio, "sleep", lambda *_: _done())
    monkeypatch.setattr(probe, "perf_counter", _clock([0.0, 0.0, 15.0]))

    result = await probe._retry_probe("bazel", lambda: _next_result(results))

    assert result == {
        "ok": False,
        "detail": "last failure",
        "latency_ms": None,
        "trace_id": None,
        "ember_session_id": None,
    }


@pytest.mark.asyncio
async def test_retry_does_not_exceed_budget(monkeypatch):
    calls = 0

    async def always_fails():
        nonlocal calls
        calls += 1
        return {"ok": False, "detail": f"failure {calls}", "latency_ms": None}

    sleeps = []
    monkeypatch.setattr(probe, "EMBER_SYNTHETIC_RETRY_BUDGET_S", 30.0)
    monkeypatch.setattr(
        probe.asyncio, "sleep", lambda interval: _sleep(sleeps, interval)
    )
    monkeypatch.setattr(probe, "perf_counter", _clock([0.0, 0.0, 15.0, 30.0]))

    result = await probe._retry_probe("bazel", always_fails)

    assert result["detail"] == "failure 2"
    assert calls == 2
    assert sleeps == [15.0]


@pytest.mark.asyncio
async def test_retry_immediate_success_has_no_retry_note(monkeypatch):
    async def succeeds():
        return {"ok": True, "detail": "warm, 501ms", "latency_ms": 501}

    result = await probe._retry_probe("bazel", succeeds)

    assert result["detail"] == "warm, 501ms"


async def _next_result(results):
    return next(results)


def _clock(values):
    values = iter(values)
    return lambda: next(values)


async def _sleep(sleeps, interval):
    sleeps.append(interval)


@pytest.fixture
def exported_spans(monkeypatch):
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(probe, "_tracer", provider.get_tracer("ember.synthetic_probe"))
    yield exporter
    provider.shutdown()


@pytest.mark.parametrize("budget", [None, "45.0"])
def test_retry_defaults_and_environment_override(monkeypatch, budget):
    # Run fresh module globals so the autouse fixture's budget cannot hide drift.
    if budget is None:
        monkeypatch.delenv("EMBER_SYNTHETIC_RETRY_BUDGET_S", raising=False)
    else:
        monkeypatch.setenv("EMBER_SYNTHETIC_RETRY_BUDGET_S", budget)
    fresh = runpy.run_path(probe.__file__)
    assert fresh["EMBER_SYNTHETIC_RETRY_BUDGET_S"] == (90.0 if budget is None else 45.0)
    assert fresh["EMBER_SYNTHETIC_RETRY_INTERVAL_S"] == 15.0


@pytest.mark.asyncio
@pytest.mark.parametrize("failures", [1, 2])
async def test_attempt_spans_recover_with_nested_work_and_sleep_gaps(
    monkeypatch, exported_spans, failures
):
    results = iter(
        [{"ok": False, "detail": "unavailable", "latency_ms": None}] * failures
        + [{"ok": True, "detail": "recovered", "latency_ms": 1}]
    )
    work_parents = []
    sleep_parents = []

    async def attempt():
        with probe._tracer.start_as_current_span("probe.work") as work:
            work_parents.append(work.get_span_context().span_id)
        return next(results)

    async def sleep(interval):
        assert interval == 15.0
        sleep_parents.append(probe.trace.get_current_span().get_span_context().span_id)
        # Each attempt is already finished before its retry delay starts.
        assert len(exported_spans.get_finished_spans()) == 2 * len(sleep_parents)

    monkeypatch.setattr(probe, "EMBER_SYNTHETIC_RETRY_BUDGET_S", 90.0)
    monkeypatch.setattr(probe.asyncio, "sleep", sleep)
    monkeypatch.setattr(
        probe, "perf_counter", _clock([0.0] + [15.0 * n for n in range(failures)])
    )
    result = await probe._retry_probe("bazel", attempt)

    spans = exported_spans.get_finished_spans()
    root = spans[-1]
    attempts = [s for s in spans if s.name == "ember.probe.bazel.attempt"]
    work = [s for s in spans if s.name == "probe.work"]
    assert len(attempts) == failures + 1
    assert [s.attributes["ember.probe.attempt.number"] for s in attempts] == list(
        range(1, failures + 2)
    )
    assert [s.attributes["ember.probe.attempt.ok"] for s in attempts] == (
        [False] * failures + [True]
    )
    assert [s.status.status_code for s in attempts] == (
        [StatusCode.ERROR] * failures + [StatusCode.UNSET]
    )
    assert all(s.parent.span_id == root.context.span_id for s in attempts)
    assert all(s.context.trace_id == root.context.trace_id for s in attempts)
    assert [s.parent.span_id for s in work] == [s.context.span_id for s in attempts]
    assert [s.context.span_id for s in work] == work_parents
    assert sleep_parents == [root.context.span_id] * failures
    assert all("ember.probe.ok" not in s.attributes for s in attempts)
    assert root.parent is None
    assert root.attributes == {
        "ember.probe.demo": "bazel",
        "ember.probe.ok": True,
        "ember.probe.retries": failures,
        "ember.probe.detail": f"recovered (recovered after {failures} retries)",
    }
    assert root.status.status_code == StatusCode.UNSET
    assert result["trace_id"] == f"{root.context.trace_id:032x}"


@pytest.mark.asyncio
async def test_failed_attempts_and_root_bound_detail(monkeypatch, exported_spans):
    detail = "x" * (probe._SPAN_DETAIL_MAX_CHARS + 100)
    original = {"ok": False, "detail": detail, "latency_ms": None}
    sleeps = []
    monkeypatch.setattr(probe, "EMBER_SYNTHETIC_RETRY_BUDGET_S", 90.0)
    monkeypatch.setattr(probe.asyncio, "sleep", lambda n: _sleep(sleeps, n))
    monkeypatch.setattr(
        probe, "perf_counter", _clock([0.0, 0.0, 15.0, 30.0, 45.0, 60.0, 75.0])
    )
    result = await probe._retry_probe(
        "postgres", lambda: _next_result(iter([original]))
    )

    spans = exported_spans.get_finished_spans()
    root = spans[-1]
    attempts = spans[:-1]
    assert len(attempts) == 6  # Attempt at 0s, then 15..75s, never at the 90s cutoff.
    assert sleeps == [15.0] * 5
    assert [s.attributes["ember.probe.attempt.number"] for s in attempts] == list(
        range(1, 7)
    )
    assert all(s.name == "ember.probe.postgres.attempt" for s in attempts)
    for span in attempts:
        assert span.parent.span_id == root.context.span_id
        assert span.attributes["ember.probe.attempt.ok"] is False
        assert span.status.status_code == StatusCode.ERROR
        assert (
            span.attributes["ember.probe.attempt.detail"]
            == (detail[: probe._SPAN_DETAIL_MAX_CHARS])
        )
        assert "ember.probe.ok" not in span.attributes
    assert root.attributes == {
        "ember.probe.demo": "postgres",
        "ember.probe.ok": False,
        "ember.probe.retries": 5,
        "ember.probe.detail": detail[: probe._SPAN_DETAIL_MAX_CHARS],
    }
    assert root.status.status_code == StatusCode.ERROR
    assert result == {
        **original,
        "trace_id": f"{root.context.trace_id:032x}",
        "ember_session_id": None,
    }
    assert original["detail"] == detail


@pytest.mark.asyncio
async def test_retry_disabled_has_one_failed_attempt(monkeypatch, exported_spans):
    async def no_sleep(_):
        pytest.fail("retry=False must not sleep")

    monkeypatch.setattr(probe, "EMBER_SYNTHETIC_RETRY_BUDGET_S", 90.0)
    monkeypatch.setattr(probe.asyncio, "sleep", no_sleep)
    result = await probe._retry_probe(
        "pages",
        lambda: _next_result(
            iter([{"ok": False, "detail": "bad title", "latency_ms": 1}])
        ),
        retry=False,
    )
    attempt, root = exported_spans.get_finished_spans()
    assert attempt.name == "ember.probe.pages.attempt"
    assert attempt.parent.span_id == root.context.span_id
    assert attempt.attributes == {
        "ember.probe.attempt.number": 1,
        "ember.probe.attempt.ok": False,
        "ember.probe.attempt.detail": "bad title",
    }
    assert attempt.status.status_code == StatusCode.ERROR
    assert root.attributes["ember.probe.retries"] == 0
    assert root.attributes["ember.probe.ok"] is False
    assert root.status.status_code == StatusCode.ERROR
    assert result["ok"] is False


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
async def test_retry_runs_share_one_independent_root_per_logical_run(
    monkeypatch, exported_spans
):
    attempts = []
    results = iter(
        [
            {"ok": False, "detail": "retry", "latency_ms": None},
            {"ok": True, "detail": "recovered", "latency_ms": 1},
        ]
    )

    async def attempt():
        attempts.append(probe._current_trace_id())
        return next(results)

    monkeypatch.setattr(probe, "EMBER_SYNTHETIC_RETRY_BUDGET_S", 30.0)
    monkeypatch.setattr(probe.asyncio, "sleep", lambda *_: _done())
    monkeypatch.setattr(probe, "perf_counter", _clock([0.0, 0.0, 15.0, 30.0]))

    with probe._tracer.start_as_current_span("request") as request:
        first = await probe._retry_probe("bazel", attempt)
        second = await probe._retry_probe(
            "pages",
            lambda: _next_result(iter([{"ok": True, "detail": "ok", "latency_ms": 1}])),
            retry=False,
        )
        assert (
            probe._current_trace_id() == f"{request.get_span_context().trace_id:032x}"
        )

    roots = [s for s in exported_spans.get_finished_spans() if s.parent is None]
    assert attempts == [first["trace_id"]] * 2
    assert first["trace_id"] != second["trace_id"]
    assert [s.name for s in roots] == [
        "ember.probe.bazel",
        "ember.probe.pages",
        "request",
    ]
    assert first["trace_id"] == f"{roots[0].context.trace_id:032x}"
    assert second["trace_id"] == f"{roots[1].context.trace_id:032x}"
    assert len({s.context.trace_id for s in roots}) == 3


@pytest.mark.asyncio
async def test_bazel_drift_is_not_ok(monkeypatch):
    async def run_query(_):
        return 200, {"analyzed_line": "1 package loaded", "wall_ms": 4}

    monkeypatch.setattr(probe.bazel_core, "run_query", run_query)
    result = await probe.probe_bazel()
    assert result["ok"] is False
    assert "0 packages loaded" in result["detail"]


@pytest.mark.asyncio
async def test_postgres_busy_is_skipped(monkeypatch):
    monkeypatch.setattr(probe.core, "demo_pg_dsn", lambda: "postgres://test")
    monkeypatch.setattr(probe.core, "try_acquire_query_slot", lambda: False)
    result = await probe.probe_postgres()
    assert result["skip"] is True


@pytest.mark.asyncio
async def test_postgres_unconfigured_is_not_ok(monkeypatch):
    monkeypatch.setattr(probe.core, "demo_pg_dsn", lambda: "")

    result = await probe.probe_postgres()

    assert result["ok"] is False
    assert result["detail"] == "DEMO_POSTGRES_DSN not configured"


@pytest.mark.asyncio
async def test_postgres_aggregate_roundtrip_success(monkeypatch):
    """Successful aggregate roundtrip reports connect_ms as latency."""
    monkeypatch.setattr(probe.core, "demo_pg_dsn", lambda: "postgres://test")
    monkeypatch.setattr(probe.core, "EMBERVM_URL", "http://test")
    monkeypatch.setattr(
        probe.core,
        "cached_demo_pg_status",
        lambda: {"state": "asleep", "generation": 1},
    )
    monkeypatch.setattr(probe.core, "try_acquire_query_slot", lambda: True)
    monkeypatch.setattr(
        probe.core,
        "demo_pg_orders_roundtrip",
        lambda *_: {"connect_ms": 42, "total_ms": 100},
    )
    monkeypatch.setattr(probe.core, "classify_wake", lambda _: "cold")
    monkeypatch.setattr(probe.core, "release_query_slot", lambda: None)

    result = await probe.probe_postgres()
    assert result["ok"] is True
    assert result["latency_ms"] == 42
    assert "cold" in result["detail"]


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
async def test_page_5xx_retried_once_then_failed(monkeypatch):
    class Response:
        status_code = 503
        text = "unavailable"

    class Client:
        calls = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, *args):
            self.calls += 1
            return Response()

    client = Client()
    monkeypatch.setenv("EMBER_SYNTHETIC_BASE_URL", "https://example.test")
    monkeypatch.setattr(probe.httpx, "AsyncClient", lambda **_: client)
    monkeypatch.setattr(probe.asyncio, "sleep", lambda *_: _done())
    result = await probe.probe_pages()
    assert client.calls == 2
    assert result["ok"] is False
    assert "/ember" in result["detail"]


async def _done():
    return None


@pytest.mark.asyncio
async def test_record_preserves_last_ok_at_on_failure(monkeypatch):
    class FakeSession:
        row = EmberSyntheticProbe(
            demo="bazel",
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
    await probe.record("bazel", {"ok": False, "detail": "failed", "latency_ms": None})
    assert session.row.ok is False
    assert session.row.last_ok_at == old
    assert session.row.trace_id is None
    assert session.row.ember_session_id is None
