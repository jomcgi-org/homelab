"""Tests for the firecracker demos router.

The router wraps the existing firecracker-backed handlers (sandbox run_python,
semgrep scan) and the SigNoz trace reader, and shapes their output for the
authenticated demos page. These tests mount ONLY the router on a bare FastAPI app
and stub every underlying handler, so nothing here reaches fc-invoke or
ClickHouse. They assert the documented payload shape, that a 32-hex trace_id is
always present on the POST endpoints, and that the trace endpoint's ``complete``
flag tracks whether spans came back.

The demo-postgres status/query/session tests moved to
ember_public/router_test.py; this file keeps only the destructive reset test
(private-only) plus the non-postgres demo tests.
"""

from __future__ import annotations

import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import demos.firecracker_api as fc
import ember_public.core as core

_HEX32 = re.compile(r"^[0-9a-f]{32}$")


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(fc.router)
    return TestClient(app)


def test_python_returns_run_shape_with_trace_id(monkeypatch):
    async def fake_run(code, files=None):
        assert code == "print(1)"
        return {
            "stdout": "1\n",
            "stderr": "",
            "exit_code": 0,
            "duration_ms": 42,
            "files": [],
            "truncated": False,
        }

    monkeypatch.setattr(fc, "run_python_in_sandbox", fake_run)

    resp = _client().post("/api/demos/firecracker/python", json={"code": "print(1)"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["stdout"] == "1\n"
    assert body["stderr"] == ""
    assert body["exit_code"] == 0
    assert body["duration_ms"] == 42
    assert _HEX32.match(body["trace_id"])


def test_semgrep_returns_findings_shape_with_trace_id(monkeypatch):
    async def fake_scan(files, dedupe=True):
        assert files == [{"path": "a.py", "content": "x = 1"}]
        return {
            "findings": [{"path": "a.py", "line": 1, "rule_id": "r", "message": "m"}],
            "errors": [],
        }

    monkeypatch.setattr(fc, "scan_files", fake_scan)

    resp = _client().post(
        "/api/demos/firecracker/semgrep",
        json={"files": [{"path": "a.py", "content": "x = 1"}]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["findings"][0]["rule_id"] == "r"
    assert body["errors"] == []
    assert isinstance(body["duration_ms"], (int, float))
    assert _HEX32.match(body["trace_id"])


def test_semgrep_demo_bypasses_idempotency_dedupe(monkeypatch):
    """The demo single-scan handler must pass dedupe=False so it always runs a
    genuinely fresh scan instead of reading a cached prior result via the
    EmberVM Idempotency-Key path (Task 4, R1)."""
    captured = {}

    async def fake_scan(files, dedupe=True):
        captured["dedupe"] = dedupe
        return {"findings": [], "errors": []}

    monkeypatch.setattr(fc, "scan_files", fake_scan)

    resp = _client().post(
        "/api/demos/firecracker/semgrep",
        json={"files": [{"path": "a.py", "content": "x = 1"}]},
    )
    assert resp.status_code == 200
    assert captured["dedupe"] is False


def test_trace_complete_true_when_spans_present(monkeypatch):
    async def fake_fetch(trace_id):
        assert trace_id == "a" * 32
        return [{"span_id": "s1", "name": "demo.python", "start_ms": 0.0}]

    async def fake_correlated(trace_id):
        return []

    monkeypatch.setattr(fc, "fetch_trace_spans", fake_fetch)
    monkeypatch.setattr(fc, "fetch_correlated_spans", fake_correlated)

    resp = _client().get(f"/api/demos/firecracker/trace/{'a' * 32}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["complete"] is True
    assert body["spans"][0]["span_id"] == "s1"
    assert body["correlated"] == []


def test_trace_incomplete_when_no_spans(monkeypatch):
    async def fake_fetch(trace_id):
        return []

    async def fake_correlated(trace_id):
        return []

    monkeypatch.setattr(fc, "fetch_trace_spans", fake_fetch)
    monkeypatch.setattr(fc, "fetch_correlated_spans", fake_correlated)

    resp = _client().get(f"/api/demos/firecracker/trace/{'b' * 32}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["complete"] is False
    assert body["spans"] == []
    assert body["correlated"] == []


def test_trace_includes_correlated_goose_spans(monkeypatch):
    """The endpoint surfaces the agent's correlated goose spans separately.

    `complete` stays keyed on the main spans only: correlated goose spans can
    stream in before or after the main trace's spans and must not flip it.
    """

    async def fake_fetch(trace_id):
        return []

    async def fake_correlated(trace_id):
        assert trace_id == "c" * 32
        return [{"span_id": "g1", "name": "reply", "service": "goose-coding"}]

    monkeypatch.setattr(fc, "fetch_trace_spans", fake_fetch)
    monkeypatch.setattr(fc, "fetch_correlated_spans", fake_correlated)

    resp = _client().get(f"/api/demos/firecracker/trace/{'c' * 32}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["complete"] is False
    assert body["spans"] == []
    assert body["correlated"][0]["span_id"] == "g1"


# ---------------------------------------------------------------------------
# Demo-postgres reset (private-only, destructive).
# ---------------------------------------------------------------------------


def test_postgres_reset_proxies_destroy(monkeypatch):
    monkeypatch.setattr(fc, "EMBERVM_URL", "http://embervm")
    monkeypatch.setattr(core, "EMBERVM_URL", "http://embervm")
    monkeypatch.setattr(core, "auth_headers", lambda: {"Authorization": "Bearer t"})

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"workload": "demo-postgres", "destroyed": 1, "evicted": 1}

    class FakeAsyncClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def delete(self, url, headers=None):
            assert url == "http://embervm/v1/stateful/demo-postgres/instance"
            assert headers == {"Authorization": "Bearer t"}
            return FakeResponse()

    monkeypatch.setattr(core.httpx, "AsyncClient", FakeAsyncClient)

    resp = _client().post("/api/demos/firecracker/postgres/reset")
    assert resp.status_code == 200
    assert resp.json() == {"destroyed": 1, "evicted": 1}
