"""Tests for the internal ember session-probe endpoints.

Every probe is mocked: the point of these tests is the endpoints' orchestration
(running the lane probe, recording it, the in-flight guards, the failure log
and notification), not the probes themselves, which are covered in
synthetic_probe_test.py.
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


@pytest.fixture
def app():
    app = FastAPI()
    app.include_router(internal_router)
    return app


@pytest.fixture
def quiet_notify(monkeypatch):
    async def notify(message, level):
        return None

    monkeypatch.setattr(synthetic_router, "_notify", notify)


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
    # Recorded, not merely returned: the latch row is what /api/health reads.
    assert recorded == {"codex": result}


def test_codex_session_probe_failure_sends_notify(app, monkeypatch):
    """A failing probe is not an endpoint error: the latch row carries it.

    The triggering job must exit 0 so Argo retries and failed-job alerts stay
    reserved for a genuinely unreachable endpoint or a failed DB write.
    """
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


@pytest.mark.parametrize("lane", ["codex", "spark"])
@pytest.mark.parametrize("trace_id", ["a" * 32, "0123456789abcdef" * 2])
def test_probe_failure_log_includes_valid_raw_trace(
    app, monkeypatch, caplog, lane, trace_id
):
    result = {
        "ok": False,
        "detail": f"boom:{lane}",
        "latency_ms": None,
        "trace_id": trace_id,
    }
    notifications = []
    recorded = {}

    async def probe():
        return result

    async def record(demo, outcome):
        recorded[demo] = outcome

    async def notify(message, level):
        notifications.append((message, level))

    monkeypatch.setattr(f"ember_public.synthetic_probe.probe_{lane}", probe)
    monkeypatch.setattr("ember_public.synthetic_probe.record", record)
    monkeypatch.setattr(synthetic_router, "_notify", notify)

    response = TestClient(app).post(f"/internal/ember/{lane}-session-probe")

    assert response.status_code == 200
    assert response.json() == {lane: result}
    assert recorded == {lane: result}
    assert notifications == [(result["detail"], "warn")]
    assert (
        f"ember synthetic {lane} failed: boom:{lane} (trace_id={trace_id})"
    ) in caplog.text
    assert "signoz" not in caplog.text
    assert "https://" not in caplog.text


@pytest.mark.parametrize("lane", ["codex", "spark"])
@pytest.mark.parametrize(
    "trace_id",
    [None, "", "0" * 32, "A" * 32, "g" * 32, "a" * 31, "a" * 33, 123, "\n" + "a" * 31],
)
def test_probe_failure_log_omits_invalid_trace(
    app, monkeypatch, quiet_notify, caplog, lane, trace_id
):
    result = {"ok": False, "detail": f"boom:{lane}", "latency_ms": None}
    if trace_id is not None:
        result["trace_id"] = trace_id

    async def probe():
        return result

    async def record(*_):
        return None

    monkeypatch.setattr(f"ember_public.synthetic_probe.probe_{lane}", probe)
    monkeypatch.setattr("ember_public.synthetic_probe.record", record)

    response = TestClient(app).post(f"/internal/ember/{lane}-session-probe")

    assert response.json() == {lane: result}
    assert f"ember synthetic {lane} failed: boom:{lane}" in caplog.text
    assert "(trace_id=" not in caplog.text
    assert "signoz" not in caplog.text


@pytest.mark.parametrize("lane", ["codex", "spark"])
def test_probe_failure_warning_carries_the_recording_span(
    monkeypatch, quiet_notify, lane
):
    async def probe():
        return {"ok": False, "detail": f"boom:{lane}", "latency_ms": None}

    async def record(*_):
        return None

    monkeypatch.setattr(f"ember_public.synthetic_probe.probe_{lane}", probe)
    monkeypatch.setattr("ember_public.synthetic_probe.record", record)
    monkeypatch.setattr(synthetic_router, f"_{lane}_probe_in_flight", False)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(_TraceContextFormatter(_PLAIN_FORMAT))
    synthetic_router.logger.addHandler(handler)
    tracer = TracerProvider().get_tracer(__name__)

    try:
        with tracer.start_as_current_span("synthetic-probe") as span:
            context = span.get_span_context()
            asyncio.run(getattr(synthetic_router, f"{lane}_session_probe_endpoint")())
    finally:
        synthetic_router.logger.removeHandler(handler)

    assert (
        f"WARNING ember_public.synthetic_router: ember synthetic {lane} failed: boom:{lane}"
        f" trace_id={context.trace_id:032x} span_id={context.span_id:016x}"
    ) in stream.getvalue()


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
    monkeypatch.setattr(synthetic_router, "_spark_probe_in_flight", True)

    response = TestClient(app).post("/internal/ember/codex-session-probe")

    assert response.json() == {"codex": result}
    assert calls == 1

    monkeypatch.setattr(synthetic_router, "_spark_probe_in_flight", False)
    monkeypatch.setattr(synthetic_router, "_codex_probe_in_flight", True)

    response = TestClient(app).post("/internal/ember/codex-session-probe")

    assert response.json() == {"skipped": True, "detail": "already running"}
    assert calls == 1


def test_in_flight_flag_is_cleared_after_a_run(app, monkeypatch):
    """A run must not wedge the guard, or every later trigger no-ops forever."""
    result = {"ok": True, "detail": "completed", "latency_ms": 1.0}

    async def probe_codex():
        return result

    async def record(*_):
        return None

    monkeypatch.setattr("ember_public.synthetic_probe.probe_codex", probe_codex)
    monkeypatch.setattr("ember_public.synthetic_probe.record", record)
    client = TestClient(app)

    client.post("/internal/ember/codex-session-probe")

    assert synthetic_router._codex_probe_in_flight is False
    assert client.post("/internal/ember/codex-session-probe").json() == {
        "codex": result
    }


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


@pytest.mark.parametrize("lane", ["codex", "spark"])
@pytest.mark.parametrize("stage", ["probe", "notify", "record"])
def test_failure_propagates_and_releases_guard(app, monkeypatch, lane, stage):
    """Probe, notification and latch exceptions must not wedge either lane."""

    async def probe():
        if stage == "probe":
            raise RuntimeError("probe down")
        return {"ok": False, "detail": "failed", "latency_ms": None}

    async def notify(*args, **kwargs):
        if stage == "notify":
            raise RuntimeError("notify down")

    async def record(demo, result):
        raise RuntimeError("record down")

    monkeypatch.setattr(f"ember_public.synthetic_probe.probe_{lane}", probe)
    monkeypatch.setattr("ember_public.synthetic_probe.record", record)
    monkeypatch.setattr(synthetic_router, "_notify", notify)

    with pytest.raises(RuntimeError, match=f"{stage} down"):
        TestClient(app).post(f"/internal/ember/{lane}-session-probe")

    assert getattr(synthetic_router, f"_{lane}_probe_in_flight") is False
