"""Tests for the internal ember synthetic-probe endpoint.

Every probe is mocked: the point of these tests is the endpoint's orchestration
(running all four, recording each, the in-flight guard), not the probes
themselves, which are covered in synthetic_probe_test.py.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

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


def test_probe_failure_still_returns_200_and_records(app, recorded):
    """A failing probe is not an endpoint error: the latch row carries it.

    The triggering job must exit 0 so Argo retries and failed-job alerts stay
    reserved for a genuinely unreachable endpoint or a failed DB write.
    """
    rows = recorded(ok=False, detail="boom")

    resp = TestClient(app).post("/internal/ember/synthetic-probe")

    assert resp.status_code == 200
    assert resp.json()["bazel"]["ok"] is False
    assert rows["bazel"]["ok"] is False


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


def test_qwen_probe_endpoint_runs_and_records(app, monkeypatch):
    recorded = {}

    async def probe_qwen():
        return {"ok": True, "detail": "completed, destroyed", "latency_ms": 1}

    async def record(demo, result):
        recorded[demo] = result

    monkeypatch.setattr("ember_public.synthetic_probe.probe_qwen", probe_qwen)
    monkeypatch.setattr("ember_public.synthetic_probe.record", record)

    response = TestClient(app).post("/internal/ember/qwen-session-probe")

    assert response.status_code == 200
    assert response.json()["qwen"]["ok"] is True
    assert recorded["qwen"]["detail"] == "completed, destroyed"


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
