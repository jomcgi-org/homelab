"""Tests for the bazel skyframe query demo router (mounted on the public app).

Mounts ONLY the router on a bare FastAPI app and stubs every underlying
handler, so nothing here reaches the embervm control plane or a real DB.
Mirrors ember_public/router_test.py's style and its savings-endpoint tests.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import ember_public.bazel_core as bazel_core
from ember_public.bazel_router import router


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


@pytest.fixture(autouse=True)
def _reset_bazel_core_module_state():
    """Mirrors bazel_core_test.py's fixture: the semaphore and rate-limit
    bucket are process-global."""

    def _reset():
        bazel_core._rate_bucket.clear()
        while bazel_core._query_semaphore._value < bazel_core._QUERY_SEMAPHORE_SIZE:
            bazel_core._query_semaphore.release()
        bazel_core._savings_cache.update(
            {"at": None, "total_analysis_s_saved": None, "as_of": None}
        )

    _reset()
    yield
    _reset()


def test_successful_query_accrues_savings(monkeypatch):
    async def fake_run_query(expr):
        return 200, {
            "labels": "//absl/strings:strings",
            "truncated": False,
            "analyzed_line": "Analyzed 13 targets (0 packages loaded, 0 targets configured).",
            "wall_ms": 310,
        }

    monkeypatch.setattr(bazel_core, "run_query", fake_run_query)

    recorded = {}

    async def fake_record(wall_ms):
        recorded["wall_ms"] = wall_ms
        return 13.49

    monkeypatch.setattr(bazel_core, "record_bazel_query_savings", fake_record)

    client = _client()
    resp = client.post(
        "/api/ember/bazel/query", json={"expression": "deps(//absl/strings)"}
    )

    assert resp.status_code == 200
    assert recorded["wall_ms"] == 310


def test_query_with_non_numeric_wall_ms_does_not_call_accrual(monkeypatch):
    async def fake_run_query(expr):
        return 200, {
            "labels": "",
            "truncated": False,
            "analyzed_line": "",
            "wall_ms": None,
        }

    monkeypatch.setattr(bazel_core, "run_query", fake_run_query)

    called = {"n": 0}

    async def fake_record(wall_ms):
        called["n"] += 1
        return None

    monkeypatch.setattr(bazel_core, "record_bazel_query_savings", fake_record)

    client = _client()
    client.post("/api/ember/bazel/query", json={"expression": "deps(//absl/strings)"})

    assert called["n"] == 0


def test_query_rejection_with_wall_ms_returns_200_and_accrues(monkeypatch):
    # A wrong cquery bazel evaluated and rejected carries a real wall_ms: it
    # rides back in-band as a 200 with {error, wall_ms}, and because bazel still
    # ran against the warm snapshot, it credits the skipped cold analysis.
    async def fake_run_query(expr):
        return 422, {"error": "ERROR: no such package", "wall_ms": 236}

    monkeypatch.setattr(bazel_core, "run_query", fake_run_query)

    recorded = {}

    async def fake_record(wall_ms):
        recorded["wall_ms"] = wall_ms
        return 13.49

    monkeypatch.setattr(bazel_core, "record_bazel_query_savings", fake_record)

    client = _client()
    resp = client.post("/api/ember/bazel/query", json={"expression": "deps(//nope)"})

    assert resp.status_code == 200
    body = resp.json()
    assert "no such package" in body["error"]
    assert body["wall_ms"] == 236
    assert recorded["wall_ms"] == 236


def test_query_rejection_without_wall_ms_does_not_accrue(monkeypatch):
    # A pre-flight validation reject (no bazel run) carries wall_ms 0: still
    # returned in-band as a 200 error, but it credits nothing.
    async def fake_run_query(expr):
        return 422, {"error": "invalid expression", "wall_ms": 0}

    monkeypatch.setattr(bazel_core, "run_query", fake_run_query)

    called = {"n": 0}

    async def fake_record(wall_ms):
        called["n"] += 1
        return None

    monkeypatch.setattr(bazel_core, "record_bazel_query_savings", fake_record)

    client = _client()
    resp = client.post("/api/ember/bazel/query", json={"expression": "deps(//nope)"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["error"] == "invalid expression"
    assert called["n"] == 0


def test_transport_error_stays_5xx_and_does_not_accrue(monkeypatch):
    # A transport error (502) / timeout (504) is a real infra failure, not a
    # visitor mistake, so it stays a 5xx and credits nothing.
    async def fake_run_query(expr):
        return 502, {"error": "could not reach the query workload"}

    monkeypatch.setattr(bazel_core, "run_query", fake_run_query)

    called = {"n": 0}

    async def fake_record(wall_ms):
        called["n"] += 1
        return None

    monkeypatch.setattr(bazel_core, "record_bazel_query_savings", fake_record)

    client = _client()
    resp = client.post("/api/ember/bazel/query", json={"expression": "deps(//nope)"})

    assert resp.status_code == 502
    assert called["n"] == 0


def test_savings_endpoint_returns_cached_value(monkeypatch):
    calls = {"n": 0}

    def fake_read():
        calls["n"] += 1
        return 128.0

    monkeypatch.setattr(bazel_core, "_read_bazel_query_savings_sync", fake_read)

    client = _client()
    first = client.get("/api/ember/bazel/savings")
    assert first.status_code == 200
    body = first.json()
    assert body["total_analysis_s_saved"] == 128.0
    assert body["as_of"]

    second = client.get("/api/ember/bazel/savings")
    assert second.json().get("total_analysis_s_saved") == 128.0

    # Second call within the 30s TTL must not re-read the DB.
    assert calls["n"] == 1


def test_savings_endpoint_refetches_after_ttl_expires(monkeypatch):
    calls = {"n": 0}

    def fake_read():
        calls["n"] += 1
        return float(calls["n"])

    monkeypatch.setattr(bazel_core, "_read_bazel_query_savings_sync", fake_read)

    fake_clock = {"t": 1000.0}
    monkeypatch.setattr(bazel_core, "monotonic", lambda: fake_clock["t"])

    client = _client()
    client.get("/api/ember/bazel/savings")
    fake_clock["t"] += bazel_core._SAVINGS_CACHE_TTL_S + 0.01
    client.get("/api/ember/bazel/savings")

    assert calls["n"] == 2
