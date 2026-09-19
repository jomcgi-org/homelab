"""Tests for the internal ember synthetic-probe endpoint.

Every probe is mocked: the point of these tests is the endpoint's orchestration
(running all four, recording each, the in-flight guard), not the probes
themselves, which are covered in synthetic_probe_test.py.
"""

import asyncio
import io
import logging

import pytest
from core.log import _PLAIN_FORMAT, _TraceContextFormatter
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace import TracerProvider

from ember_public import synthetic_router
from ember_public.synthetic_router import internal_router

DEMOS = ("bazel", "semgrep", "pages", "postgres")


@pytest.fixture
def app():
    app = FastAPI()
    app.include_router(internal_router)
    return app


@pytest.fixture
def recorded(monkeypatch):
    """Mock all four probes plus record(); return the recorded {demo: result}."""
    rows: dict[str, dict] = {}

    def install(ok: bool = True, detail: str = "test"):
        for demo in DEMOS:

            async def probe(_demo=demo):
                return {"ok": ok, "detail": f"{detail}:{_demo}", "latency_ms": 10.0}

            monkeypatch.setattr(f"ember_public.synthetic_probe.probe_{demo}", probe)

        async def record(demo, result):
            rows[demo] = result

        monkeypatch.setattr("ember_public.synthetic_probe.record", record)
        return rows

    return install


def test_runs_every_probe_and_records_each(app, recorded):
    rows = recorded()

    resp = TestClient(app).post("/internal/ember/synthetic-probe")

    assert resp.status_code == 200
    assert set(resp.json()) == set(DEMOS)
    # Recorded, not merely returned: the latch row is what /api/health reads.
    assert set(rows) == set(DEMOS)
    assert rows["bazel"]["detail"] == "test:bazel"


def test_probe_failure_still_returns_200_and_records(app, recorded, caplog):
    """A failing probe is not an endpoint error: the latch row carries it.

    The triggering job must exit 0 so Argo retries and failed-job alerts stay
    reserved for a genuinely unreachable endpoint or a failed DB write.
    """
    rows = recorded(ok=False, detail="boom")

    resp = TestClient(app).post("/internal/ember/synthetic-probe")

    assert resp.status_code == 200
    assert resp.json()["bazel"]["ok"] is False
    assert rows["bazel"]["ok"] is False
    assert "/app/signoz/trace/" not in caplog.text


def test_probe_failure_log_includes_valid_trace_link(
    app, recorded, monkeypatch, caplog
):
    rows = recorded(ok=False, detail="boom")
    trace_id = "a" * 32

    async def probe_bazel():
        return {
            "ok": False,
            "detail": "boom:bazel",
            "latency_ms": None,
            "trace_id": trace_id,
        }

    monkeypatch.setattr("ember_public.synthetic_probe.probe_bazel", probe_bazel)

    response = TestClient(app).post("/internal/ember/synthetic-probe")

    assert response.status_code == 200
    assert rows["bazel"]["trace_id"] == trace_id
    assert (
        f"ember synthetic bazel failed: boom:bazel "
        f"(https://private.jomcgi.dev/app/signoz/trace/{trace_id})"
    ) in caplog.text


def test_probe_failure_warning_carries_the_recording_span(recorded, monkeypatch):
    recorded(ok=False, detail="boom")
    monkeypatch.setattr(synthetic_router, "_probe_in_flight", False)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(_TraceContextFormatter(_PLAIN_FORMAT))
    synthetic_router.logger.addHandler(handler)
    tracer = TracerProvider().get_tracer(__name__)

    try:
        with tracer.start_as_current_span("synthetic-probe") as span:
            context = span.get_span_context()
            asyncio.run(synthetic_router.synthetic_probe_endpoint())
    finally:
        synthetic_router.logger.removeHandler(handler)

    assert (
        "WARNING ember_public.synthetic_router: ember synthetic bazel failed: boom:bazel"
        f" trace_id={context.trace_id:032x} span_id={context.span_id:016x}"
    ) in stream.getvalue()


def test_trigger_while_in_flight_is_a_noop(app, recorded, monkeypatch):
    """The guard short-circuits without running or recording anything."""
    rows = recorded()
    monkeypatch.setattr(synthetic_router, "_probe_in_flight", True)

    resp = TestClient(app).post("/internal/ember/synthetic-probe")

    assert resp.status_code == 200
    assert resp.json() == {"skipped": True, "detail": "already running"}
    assert rows == {}


def test_in_flight_flag_is_cleared_after_a_run(app, recorded):
    """A run must not wedge the guard, or every later trigger no-ops forever."""
    recorded()
    client = TestClient(app)

    client.post("/internal/ember/synthetic-probe")

    assert synthetic_router._probe_in_flight is False
    assert set(client.post("/internal/ember/synthetic-probe").json()) == set(DEMOS)


def test_codex_session_probe_success(app, monkeypatch):
    result = {"ok": True, "detail": "completed, destroyed", "latency_ms": 1.0}
    recorded = {}

    async def probe_codex():
        return result

    async def record(demo, outcome):
        recorded[demo] = outcome

    monkeypatch.setattr("ember_public.synthetic_probe.probe_codex", probe_codex)
    monkeypatch.setattr("ember_public.synthetic_probe.record", record)

    response = TestClient(app).post("/internal/ember/codex-session-probe")

    assert response.status_code == 200
    assert response.json() == {"codex": result}
    assert recorded == {"codex": result}


def test_codex_session_probe_failure_sends_notify(app, monkeypatch):
    detail = "Codex lane failed verbatim"
    result = {"ok": False, "detail": detail, "latency_ms": None}
    notifications = []
    recorded = {}

    async def probe_codex():
        return result

    async def notify(message, level):
        notifications.append({"message": message, "level": level})

    async def record(demo, outcome):
        recorded[demo] = outcome

    monkeypatch.setattr("ember_public.synthetic_probe.probe_codex", probe_codex)
    monkeypatch.setattr("ember_public.synthetic_probe.record", record)
    monkeypatch.setattr(synthetic_router, "_notify", notify)

    response = TestClient(app).post("/internal/ember/codex-session-probe")

    assert response.status_code == 200
    assert response.json() == {"codex": result}
    assert notifications == [{"message": detail, "level": "warn"}]
    assert recorded == {"codex": result}


def test_codex_probe_independent_guard(app, monkeypatch):
    calls = 0
    result = {"ok": True, "detail": "completed", "latency_ms": 1.0}

    async def probe_codex():
        nonlocal calls
        calls += 1
        return result

    async def record(*_):
        return None

    monkeypatch.setattr("ember_public.synthetic_probe.probe_codex", probe_codex)
    monkeypatch.setattr("ember_public.synthetic_probe.record", record)
    monkeypatch.setattr(synthetic_router, "_probe_in_flight", True)

    response = TestClient(app).post("/internal/ember/codex-session-probe")

    assert response.json() == {"codex": result}
    assert calls == 1

    monkeypatch.setattr(synthetic_router, "_probe_in_flight", False)
    monkeypatch.setattr(synthetic_router, "_codex_probe_in_flight", True)

    response = TestClient(app).post("/internal/ember/codex-session-probe")

    assert response.json() == {"skipped": True, "detail": "already running"}
    assert calls == 1


def test_spark_session_probe_success(app, monkeypatch):
    result = {"ok": True, "detail": "completed, destroyed", "latency_ms": 1.0}
    recorded = {}

    async def probe_spark():
        return result

    async def record(demo, outcome):
        recorded[demo] = outcome

    monkeypatch.setattr("ember_public.synthetic_probe.probe_spark", probe_spark)
    monkeypatch.setattr("ember_public.synthetic_probe.record", record)

    response = TestClient(app).post("/internal/ember/spark-session-probe")

    assert response.status_code == 200
    assert response.json() == {"spark": result}
    assert recorded == {"spark": result}


def test_spark_session_probe_failure_sends_notify(app, monkeypatch):
    detail = "Spark lane failed verbatim"
    result = {"ok": False, "detail": detail, "latency_ms": None}
    notifications = []

    async def probe_spark():
        return result

    async def record(*_):
        return None

    async def notify(message, level):
        notifications.append({"message": message, "level": level})

    monkeypatch.setattr("ember_public.synthetic_probe.probe_spark", probe_spark)
    monkeypatch.setattr("ember_public.synthetic_probe.record", record)
    monkeypatch.setattr(synthetic_router, "_notify", notify)

    response = TestClient(app).post("/internal/ember/spark-session-probe")

    assert response.status_code == 200
    assert response.json() == {"spark": result}
    assert notifications == [{"message": detail, "level": "warn"}]


def test_spark_probe_has_independent_guard(app, monkeypatch):
    calls = 0
    result = {"ok": True, "detail": "completed", "latency_ms": 1.0}

    async def probe_spark():
        nonlocal calls
        calls += 1
        return result

    async def record(*_):
        return None

    monkeypatch.setattr("ember_public.synthetic_probe.probe_spark", probe_spark)
    monkeypatch.setattr("ember_public.synthetic_probe.record", record)
    monkeypatch.setattr(synthetic_router, "_codex_probe_in_flight", True)

    response = TestClient(app).post("/internal/ember/spark-session-probe")

    assert response.json() == {"spark": result}
    assert calls == 1

    monkeypatch.setattr(synthetic_router, "_codex_probe_in_flight", False)
    monkeypatch.setattr(synthetic_router, "_spark_probe_in_flight", True)

    response = TestClient(app).post("/internal/ember/spark-session-probe")

    assert response.json() == {"skipped": True, "detail": "already running"}
    assert calls == 1


def test_record_failure_propagates(app, monkeypatch):
    """A failed write must surface, so the job fails rather than going blind."""
    for demo in DEMOS:

        async def probe(_demo=demo):
            return {"ok": True, "detail": "ok", "latency_ms": 1.0}

        monkeypatch.setattr(f"ember_public.synthetic_probe.probe_{demo}", probe)

    async def record(demo, result):
        raise RuntimeError("db down")

    monkeypatch.setattr("ember_public.synthetic_probe.record", record)

    with pytest.raises(RuntimeError, match="db down"):
        TestClient(app).post("/internal/ember/synthetic-probe")

    # The guard must still be released when recording blew up mid-run.
    assert synthetic_router._probe_in_flight is False
