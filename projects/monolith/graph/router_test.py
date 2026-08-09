from fastapi import FastAPI
from fastapi.testclient import TestClient

import graph.router as graph_router


def client():
    app = FastAPI()
    app.include_router(graph_router.router)
    return TestClient(app)


def test_disabled_returns_503(monkeypatch):
    monkeypatch.setenv("GRAPH_ENABLED", "false")
    response = client().post(
        "/api/graph/runs",
        json={"task": "fix", "repo": "jomcgi/homelab", "branch": "main"},
    )
    assert response.status_code == 503
    assert "disabled" in response.json()["detail"]


def test_unknown_repo_rejected(monkeypatch):
    monkeypatch.setenv("GRAPH_ENABLED", "true")
    response = client().post(
        "/api/graph/runs",
        json={"task": "fix", "repo": "not/a-repo", "branch": "main"},
    )
    assert response.status_code == 400
    assert "unknown repo" in response.json()["detail"]


def test_start_run(monkeypatch):
    monkeypatch.setenv("GRAPH_ENABLED", "true")

    class Handle:
        workflow_id = "wf-1"

    class FakeDBOS:
        def start_workflow(self, *args):
            return Handle()

    monkeypatch.setattr(graph_router.runtime, "init_dbos", lambda: FakeDBOS())
    response = client().post(
        "/api/graph/runs",
        json={
            "task": "fix",
            "repo": "jomcgi/homelab",
            "branch": "main",
            "idempotency_key": "request-1",
        },
    )
    assert response.status_code == 200
    assert response.json() == {"workflow_id": "wf-1"}
