"""Tests for the firecracker demos router.

The router wraps the existing firecracker-backed handlers (sandbox run_python,
semgrep scan, goosecracker submit/poll) and the SigNoz trace reader, and shapes
their output for the authenticated demos page. These tests mount ONLY the router
on a bare FastAPI app and stub every underlying handler, so nothing here reaches
fc-invoke or ClickHouse. They assert the documented payload shape, that a 32-hex
trace_id is always present on the POST endpoints, and that the trace endpoint's
``complete`` flag tracks whether spans came back.
"""

from __future__ import annotations

import re

from fastapi import FastAPI
from fastapi.testclient import TestClient

import demos.firecracker_api as fc

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


def test_goose_submit_returns_thread_id_with_trace_id(monkeypatch):
    def fake_submit(task, *, session, recipe, tier, **kwargs):
        assert task == "do a thing"
        return {"session": session, "thread_id": "t-abc123", "action": "create"}

    monkeypatch.setattr(fc.goosecracker, "submit", fake_submit)

    resp = _client().post(
        "/api/demos/firecracker/goose",
        json={"task": "do a thing", "recipe": "agent", "tier": ""},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["thread_id"] == "t-abc123"
    assert body["session"]
    assert _HEX32.match(body["trace_id"])


def test_goose_poll_running(monkeypatch):
    def fake_get_run(thread_id):
        assert thread_id == "t-abc123"
        return {"thread_id": thread_id, "state": "RUNNING", "result": None}

    monkeypatch.setattr(fc.goosecracker, "get_run", fake_get_run)
    monkeypatch.setattr(fc.goosecracker, "serialize", lambda row: dict(row))

    resp = _client().get("/api/demos/firecracker/goose/t-abc123")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "RUNNING"
    assert body["done"] is False


def test_goose_poll_done_carries_result(monkeypatch):
    def fake_get_run(thread_id):
        return {"thread_id": thread_id, "state": "COMPLETED", "result": "final answer"}

    monkeypatch.setattr(fc.goosecracker, "get_run", fake_get_run)
    monkeypatch.setattr(fc.goosecracker, "serialize", lambda row: dict(row))

    resp = _client().get("/api/demos/firecracker/goose/t-xyz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "COMPLETED"
    assert body["done"] is True
    assert body["result"] == "final answer"


def test_goose_poll_unknown_thread_returns_404(monkeypatch):
    monkeypatch.setattr(fc.goosecracker, "get_run", lambda thread_id: None)

    resp = _client().get("/api/demos/firecracker/goose/t-missing")
    assert resp.status_code == 404


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
# Demo-postgres (stateful sleep/wake exhibit)
# ---------------------------------------------------------------------------


def _pg_status_payload(**overrides):
    base = {
        "workload": "demo-postgres",
        "state": "banked",
        "generation": 7,
        "bundle_generation": 7,
        "pair_valid": True,
        "volume_bytes": 123456,
        "instance": {
            "healthy": True,
            "last_active_at": "2026-07-17T10:00:00Z",
            "created_at": "2026-07-17T09:00:00Z",
        },
    }
    base.update(overrides)
    return base


def test_postgres_status_unconfigured(monkeypatch):
    monkeypatch.delenv("DEMO_POSTGRES_DSN", raising=False)

    resp = _client().get("/api/demos/firecracker/postgres/status")
    assert resp.status_code == 200
    assert resp.json() == {"configured": False}


def test_postgres_status_shapes_control_plane_payload(monkeypatch):
    monkeypatch.setenv("DEMO_POSTGRES_DSN", "postgresql://x")
    monkeypatch.setattr(fc, "EMBERVM_URL", "http://embervm")

    async def fake_status():
        return _pg_status_payload()

    monkeypatch.setattr(fc, "_fetch_demo_pg_status", fake_status)

    resp = _client().get("/api/demos/firecracker/postgres/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["configured"] is True
    assert body["state"] == "banked"
    assert body["generation"] == 7
    assert body["bundle_generation"] == 7
    assert body["pair_valid"] is True
    assert body["healthy"] is True
    assert body["last_active_at"] == "2026-07-17T10:00:00Z"


def test_postgres_status_error_is_in_band(monkeypatch):
    """A flaky control-plane poll must not 5xx: the frontend polls sub-second."""
    monkeypatch.setenv("DEMO_POSTGRES_DSN", "postgresql://x")
    monkeypatch.setattr(fc, "EMBERVM_URL", "http://embervm")

    async def fake_status():
        raise RuntimeError("boom")

    monkeypatch.setattr(fc, "_fetch_demo_pg_status", fake_status)

    resp = _client().get("/api/demos/firecracker/postgres/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["configured"] is True
    assert "boom" in body["error"]


def test_postgres_query_unconfigured_is_503(monkeypatch):
    monkeypatch.delenv("DEMO_POSTGRES_DSN", raising=False)

    resp = _client().post("/api/demos/firecracker/postgres/query", json={})
    assert resp.status_code == 503


def test_postgres_query_returns_timings_and_classification(monkeypatch):
    monkeypatch.setenv("DEMO_POSTGRES_DSN", "postgresql://x")
    monkeypatch.setattr(fc, "EMBERVM_URL", "http://embervm")

    async def fake_status():
        return _pg_status_payload(state="banked", pair_valid=True)

    def fake_roundtrip(dsn, mode):
        assert dsn == "postgresql://x"
        assert mode == "insert"
        return {
            "connect_ms": 850.0,
            "query_ms": 12.0,
            "mode": mode,
            "statements": [
                {"sql": "CREATE TABLE IF NOT EXISTS demo_orders (...)", "ms": 1.0},
                {"sql": "INSERT INTO demo_orders (...)", "ms": 2.0},
                {"sql": "SELECT ... FROM demo_orders ORDER BY id DESC", "ms": 3.0},
                {"sql": "SELECT item, sum(qty) ... GROUP BY item", "ms": 4.0},
                {"sql": "SELECT count(*), coalesce(sum(...)), ...", "ms": 5.0},
            ],
            "inserted": {
                "id": 42,
                "item": "flat white",
                "qty": 2,
                "unit_price": 3.50,
            },
            "rows": [
                {
                    "id": 42,
                    "item": "flat white",
                    "qty": 2,
                    "unit_price": 3.50,
                    "written_at": "2026-07-17T08:00:00+00:00",
                    "postmaster_start": "2026-07-17T08:00:00+00:00",
                }
            ],
            "breakdown": [{"item": "flat white", "units": 2, "revenue": 7.0}],
            "total_orders": 42,
            "total_revenue": 7.0,
            "postmaster_start": "2026-07-17T08:00:00+00:00",
        }

    monkeypatch.setattr(fc, "_fetch_demo_pg_status", fake_status)
    monkeypatch.setattr(fc, "_demo_pg_orders_roundtrip", fake_roundtrip)

    resp = _client().post(
        "/api/demos/firecracker/postgres/query", json={"mode": "insert"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["error"] is None
    assert body["connect_ms"] == 850.0
    assert body["query_ms"] == 12.0
    assert body["mode"] == "insert"
    assert body["classification"] == "relight"
    assert body["phase_before"] == "banked"
    assert body["generation"] == 7
    assert body["rows"][0]["id"] == 42
    assert len(body["statements"]) == 5
    assert body["breakdown"][0]["item"] == "flat white"
    assert body["total_revenue"] == 7.0
    assert body["total_ms"] >= 0


def test_postgres_query_aggregate_mode(monkeypatch):
    """Aggregate mode is read-only: it wakes the VM but writes nothing."""
    monkeypatch.setenv("DEMO_POSTGRES_DSN", "postgresql://x")
    monkeypatch.setattr(fc, "EMBERVM_URL", "http://embervm")

    async def fake_status():
        return _pg_status_payload(state="serving")

    def fake_roundtrip(dsn, mode):
        assert mode == "aggregate"
        return {
            "connect_ms": 1.5,
            "query_ms": 3.0,
            "mode": mode,
            "statements": [],
            "inserted": None,
            "rows": [],
            "breakdown": [],
            "total_orders": 0,
            "total_revenue": 0.0,
            "postmaster_start": "2026-07-17T08:00:00+00:00",
        }

    monkeypatch.setattr(fc, "_fetch_demo_pg_status", fake_status)
    monkeypatch.setattr(fc, "_demo_pg_orders_roundtrip", fake_roundtrip)

    resp = _client().post(
        "/api/demos/firecracker/postgres/query", json={"mode": "aggregate"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "aggregate"
    assert body["inserted"] is None


def test_postgres_query_default_mode_is_insert(monkeypatch):
    monkeypatch.setenv("DEMO_POSTGRES_DSN", "postgresql://x")
    monkeypatch.setattr(fc, "EMBERVM_URL", "http://embervm")

    async def fake_status():
        return _pg_status_payload(state="serving")

    def fake_roundtrip(dsn, mode):
        assert mode == "insert"
        return {
            "connect_ms": 1.5,
            "query_ms": 3.0,
            "mode": mode,
            "statements": [],
            "inserted": {"id": 1, "item": "flat white", "qty": 1, "unit_price": 3.50},
            "rows": [],
            "breakdown": [],
            "total_orders": 1,
            "total_revenue": 3.50,
            "postmaster_start": "2026-07-17T08:00:00+00:00",
        }

    monkeypatch.setattr(fc, "_fetch_demo_pg_status", fake_status)
    monkeypatch.setattr(fc, "_demo_pg_orders_roundtrip", fake_roundtrip)

    resp = _client().post("/api/demos/firecracker/postgres/query", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "insert"


def test_postgres_query_connect_failure_is_in_band(monkeypatch):
    """A refused connect (wake-rate limit / failed wake) reports, not 5xxs."""
    monkeypatch.setenv("DEMO_POSTGRES_DSN", "postgresql://x")
    monkeypatch.setattr(fc, "EMBERVM_URL", "http://embervm")

    async def fake_status():
        return _pg_status_payload(state="serving")

    def fake_roundtrip(dsn, mode):
        raise OSError("connection refused")

    monkeypatch.setattr(fc, "_fetch_demo_pg_status", fake_status)
    monkeypatch.setattr(fc, "_demo_pg_orders_roundtrip", fake_roundtrip)

    resp = _client().post("/api/demos/firecracker/postgres/query", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert "connection refused" in body["error"]
    assert body["mode"] == "insert"
    assert body["classification"] == "warm"


def test_classify_wake_paths():
    assert fc._classify_wake(None) == "unknown"
    assert fc._classify_wake({"state": "serving"}) == "warm"
    assert fc._classify_wake({"state": "banked", "pair_valid": True}) == "relight"
    assert fc._classify_wake({"state": "banked", "pair_valid": False}) == "cold"
    assert fc._classify_wake({"state": None}) == "cold"
    assert fc._classify_wake({"state": "relighting"}) == "transitional"


def test_postgres_reset_proxies_destroy(monkeypatch):
    monkeypatch.setattr(fc, "EMBERVM_URL", "http://embervm")
    monkeypatch.setattr(fc, "auth_headers", lambda: {"Authorization": "Bearer t"})

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

    monkeypatch.setattr(fc.httpx, "AsyncClient", FakeAsyncClient)

    resp = _client().post("/api/demos/firecracker/postgres/reset")
    assert resp.status_code == 200
    assert resp.json() == {"destroyed": 1, "evicted": 1}
