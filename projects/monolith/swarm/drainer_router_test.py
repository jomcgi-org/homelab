from fastapi import FastAPI
from fastapi.testclient import TestClient

import swarm.drainer_router as drainer_router


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(drainer_router.router)
    return TestClient(app)


def test_disabled_returns_200(monkeypatch):
    monkeypatch.setattr(drainer_router, "drainer_enabled", lambda: False)

    response = _client().post("/internal/agent/drain")

    assert response.status_code == 200
    assert response.json() == {"status": "disabled"}


def test_dbos_not_launched_returns_503(monkeypatch):
    monkeypatch.setattr(drainer_router, "drainer_enabled", lambda: True)
    monkeypatch.setattr(drainer_router.runtime, "is_launched", lambda: False)

    response = _client().post("/internal/agent/drain")

    assert response.status_code == 503
    assert "not launched" in response.json()["detail"]


def test_launched_enqueues_workflow_on_drainer_queue(monkeypatch):
    enqueued = []

    class FakeDBOS:
        def start_workflow(self, _workflow):
            raise AssertionError("drain cycle must be queued")

    class FakeQueue:
        def enqueue(self, workflow):
            enqueued.append(workflow)

    monkeypatch.setattr(drainer_router, "drainer_enabled", lambda: True)
    monkeypatch.setattr(drainer_router.runtime, "is_launched", lambda: True)
    monkeypatch.setattr(drainer_router.runtime, "init_dbos", lambda: FakeDBOS())
    monkeypatch.setattr(drainer_router, "drainer_queue", lambda: FakeQueue())

    response = _client().post("/internal/agent/drain")

    assert response.status_code == 202
    assert response.json() == {"status": "started"}
    assert enqueued == [drainer_router.drain_cycle]


def test_drainer_flag_enables_shared_dbos_runtime(monkeypatch):
    monkeypatch.setattr(drainer_router.runtime.config, "enabled", lambda: False)
    monkeypatch.setenv("DRAINER_ENABLED", "true")

    assert drainer_router.runtime._enabled() is True
