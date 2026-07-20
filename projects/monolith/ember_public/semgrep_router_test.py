"""Tests for the semgrep scan demo router (mounted on the public app).

Mounts ONLY the router on a bare FastAPI app and fakes
``semgrep_scan.client.scan_files`` plus the savings accrual, so nothing here
reaches fc-invoke or a real DB. Mirrors ember_public/bazel_router_test.py's
style.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import ember_public.semgrep_core as semgrep_core
from ember_public.semgrep_router import _DEMO_SG_SESSION_COOKIE, router


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


@pytest.fixture(autouse=True)
def _reset_semgrep_core_module_state():
    """The rate bucket and queue are process-global; reset around each test."""

    def _reset():
        semgrep_core._scan_buckets.clear()
        semgrep_core.QUEUE = semgrep_core._make_queue(slots=3, max_waiters=8)
        semgrep_core._savings_cache.update(
            {
                "at": None,
                "scans": None,
                "actual_ms": None,
                "saved_ms": None,
                "as_of": None,
            }
        )

    _reset()
    yield
    _reset()


def _fake_scan_files_module(monkeypatch, fake):
    """semgrep_router imports semgrep_scan.client lazily inside the handler,
    so patch the source module rather than an attribute on semgrep_router."""
    import semgrep_scan.client as client_module

    monkeypatch.setattr(client_module, "scan_files", fake)


def test_scan_without_session_returns_401():
    client = _client()
    resp = client.post(
        "/api/ember/semgrep/scan", json={"language": "python", "content": "x = 1\n"}
    )
    assert resp.status_code == 401


def test_scan_oversize_snippet_returns_422():
    client = _client()
    client.cookies.set(_DEMO_SG_SESSION_COOKIE, "sess-1")
    resp = client.post(
        "/api/ember/semgrep/scan",
        json={"language": "python", "content": "x = 1\n" * 300},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert "lines" in body["detail"]


def test_scan_rapid_repeat_returns_429(monkeypatch):
    async def fake_scan(files, dedupe=True):
        return {"findings": [], "errors": []}

    _fake_scan_files_module(monkeypatch, fake_scan)

    client = _client()
    client.cookies.set(_DEMO_SG_SESSION_COOKIE, "sess-rate")
    body = {"language": "python", "content": "import os\n"}

    first = client.post("/api/ember/semgrep/scan", json=body)
    assert first.status_code == 200

    second = client.post("/api/ember/semgrep/scan", json=body)
    assert second.status_code == 429
    payload = second.json()
    assert payload["retry_after_s"] == 3


def test_scan_returns_503_when_queue_full(monkeypatch):
    # Synchronously drive the queue to "every slot and every waiting position
    # taken" (slots=1, max_waiters=0, one held slot) without an active
    # `async with`, since TestClient runs its own event loop and cannot be
    # nested inside one that is already holding the slot.
    semgrep_core.QUEUE = semgrep_core._make_queue(slots=1, max_waiters=0)
    semgrep_core.QUEUE._sem._value = 0  # simulate the sole slot being held

    async def fake_scan(files, dedupe=True):
        return {"findings": [], "errors": []}

    _fake_scan_files_module(monkeypatch, fake_scan)

    client = _client()
    client.cookies.set(_DEMO_SG_SESSION_COOKIE, "sess-busy")
    resp = client.post(
        "/api/ember/semgrep/scan",
        json={"language": "python", "content": "import os\n"},
    )

    assert resp.status_code == 503
    body = resp.json()
    assert body["busy"] is True
    assert body["waiting"] == 0


def test_scan_success_passes_through_findings_and_accrues_savings(monkeypatch):
    async def fake_scan(files, dedupe=True):
        assert files == [{"path": "snippet.py", "content": "import os\n"}]
        assert dedupe is False
        return {
            "findings": [
                {
                    "path": "snippet.py",
                    "line": 1,
                    "col": 1,
                    "rule_id": "python.lang.security.some-rule",
                    "severity": "ERROR",
                    "message": "danger",
                }
            ],
            "errors": [],
        }

    _fake_scan_files_module(monkeypatch, fake_scan)

    recorded = {}

    async def fake_record(scan_ms):
        recorded["scan_ms"] = scan_ms
        return {"scans": 1, "actual_ms": scan_ms, "saved_ms": 0}

    monkeypatch.setattr(semgrep_core, "record_demo_sg_savings", fake_record)

    client = _client()
    client.cookies.set(_DEMO_SG_SESSION_COOKIE, "sess-ok")
    resp = client.post(
        "/api/ember/semgrep/scan",
        json={"language": "python", "content": "import os\n"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["findings"]) == 1
    assert body["findings"][0]["rule_id"] == "python.lang.security.some-rule"
    assert body["errors"] == []
    assert body["cold_start_ms"] == semgrep_core.COLD_START_MS
    assert "scan_ms" in body and "queued_ms" in body and "saved_ms" in body
    assert "scan_ms" in recorded


def test_scan_client_error_returns_502_and_does_not_accrue(monkeypatch):
    async def fake_scan(files, dedupe=True):
        return {"error": "could not reach fc-invoke: connection refused"}

    _fake_scan_files_module(monkeypatch, fake_scan)

    called = {"n": 0}

    async def fake_record(scan_ms):
        called["n"] += 1
        return None

    monkeypatch.setattr(semgrep_core, "record_demo_sg_savings", fake_record)

    client = _client()
    client.cookies.set(_DEMO_SG_SESSION_COOKIE, "sess-err")
    resp = client.post(
        "/api/ember/semgrep/scan",
        json={"language": "python", "content": "import os\n"},
    )

    assert resp.status_code == 502
    body = resp.json()
    assert "fc-invoke" in body["error"]
    assert called["n"] == 0


def test_savings_endpoint_returns_cached_shape(monkeypatch):
    async def fake_cached():
        return {"scans": 5, "actual_ms": 4500, "saved_ms": 50500, "as_of": "now"}

    monkeypatch.setattr(semgrep_core, "cached_demo_sg_savings", fake_cached)

    client = _client()
    resp = client.get("/api/ember/semgrep/savings")
    assert resp.status_code == 200
    body = resp.json()
    assert body["scans"] == 5
