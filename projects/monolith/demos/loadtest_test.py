"""Tests for the workload-parametric load-test drain (demos/loadtest.py).

These exercise the pure logic with no real DB and no fc-invoke: ``_invoke`` is
tested against a fake ``httpx.AsyncClient``, the workload registry payload
builders are asserted directly, and the drain runs against an injected fake
``_invoke`` and an in-memory ``FakeLoadStore`` for a fraction of a second.
"""

from __future__ import annotations

import asyncio

import pytest

import demos.loadtest as loadtest


class _FakeResponse:
    def __init__(self, status_code, json_body, headers):
        self.status_code = status_code
        self._json = json_body
        self.headers = headers
        self.text = ""

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx

            raise httpx.HTTPStatusError(
                "err",
                request=None,
                response=self,  # type: ignore[arg-type]
            )


class _FakeAsyncClient:
    """Minimal stand-in for httpx.AsyncClient used by ``_invoke``."""

    def __init__(self, response, **_kwargs):
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def post(self, url, json=None, headers=None):  # noqa: A002
        return self._response


@pytest.mark.asyncio
async def test_invoke_parses_resource_headers(monkeypatch):
    resp = _FakeResponse(
        200,
        {"findings": [], "errors": []},
        {
            "X-Fc-Cpu-Ms": "842",
            "X-Fc-Peak-Rss-Mib": "310",
            "X-Fc-Queue-Wait-Ms": "5",
        },
    )
    monkeypatch.setattr(loadtest, "FC_INVOKE_URL", "http://fc")
    monkeypatch.setattr(
        loadtest.httpx, "AsyncClient", lambda **kw: _FakeAsyncClient(resp, **kw)
    )

    body, meta, error = await loadtest._invoke("semgrep", {"files": []}, 95.0)

    assert error is None
    assert body == {"findings": [], "errors": []}
    assert meta == {"cpu_ms": 842, "peak_rss_mib": 310, "queue_wait_ms": 5}


@pytest.mark.asyncio
async def test_invoke_missing_headers_are_none(monkeypatch):
    resp = _FakeResponse(200, {"stdout": "hi"}, {})
    monkeypatch.setattr(loadtest, "FC_INVOKE_URL", "http://fc")
    monkeypatch.setattr(
        loadtest.httpx, "AsyncClient", lambda **kw: _FakeAsyncClient(resp, **kw)
    )

    body, meta, error = await loadtest._invoke("sandbox", {"code": "x"}, 40.0)

    assert error is None
    assert body == {"stdout": "hi"}
    assert meta == {"cpu_ms": None, "peak_rss_mib": None, "queue_wait_ms": None}


def test_semgrep_registry_builds_files_payload():
    item = {"name": "python", "path": "corpus/app.py", "content": "print(1)"}
    payload = loadtest.WORKLOADS["semgrep"]["build_payload"](item)
    assert payload == {"files": [{"path": "corpus/app.py", "content": "print(1)"}]}


def test_sandbox_registry_builds_code_payload():
    item = {"name": "hello", "content": "print(1)"}
    payload = loadtest.WORKLOADS["sandbox"]["build_payload"](item)
    assert payload == {"code": "print(1)"}


class FakeLoadStore(loadtest.LoadStore):
    """In-memory store: captures recorded rows and the finalize summary."""

    def __init__(self, run_id="run-1", workload="semgrep"):
        super().__init__(run_id, workload)
        self.rows: list[dict] = []
        self.summary: dict | None = None

    async def record(self, row: dict) -> None:
        self.rows.append(row)

    async def flush(self) -> None:  # no DB
        return None

    async def finalize(self, run_id: str) -> dict:
        self.summary = loadtest.build_summary(
            self.workload, self.rows, self._sampler_series
        )
        return self.summary


@pytest.mark.asyncio
async def test_drain_records_rows_with_meta():
    async def fake_invoke(workload, payload, timeout):
        return (
            {"findings": [{"check_id": "x"}], "errors": []},
            {"cpu_ms": 800, "peak_rss_mib": 300, "queue_wait_ms": 2},
            None,
        )

    async def fake_sampler(stop_event):
        return []

    store = FakeLoadStore(run_id="run-1", workload="semgrep")

    await loadtest.run_load_test(
        "run-1",
        "semgrep",
        store,
        duration_s=1,
        client_concurrency=4,
        invoke=fake_invoke,
        sampler=fake_sampler,
    )

    assert store.rows, "expected recorded scan rows"
    for row in store.rows:
        assert row["status"] == "ok"
        assert row["cpu_ms"] == 800
        assert row["peak_rss_mib"] == 300
        assert row["result_count"] == 1

    assert store.summary is not None
    for key in (
        "total_scans",
        "errors",
        "wall_s",
        "throughput_per_s",
        "latency_ms",
        "queue_wait_ms",
        "per_scan_cpu_ms",
        "per_scan_peak_rss_mib",
        "per_lang",
        "daemon",
        "extrapolation",
    ):
        assert key in store.summary
    # No sampler series -> daemon footprint is derived, node stats omitted.
    assert store.summary["daemon"]["source"] == "derived"


@pytest.mark.asyncio
async def test_drain_records_error_rows_without_raising():
    async def failing_invoke(workload, payload, timeout):
        return {}, {}, "HTTP 500: boom"

    async def fake_sampler(stop_event):
        return []

    store = FakeLoadStore(workload="sandbox")
    await loadtest.run_load_test(
        "run-2",
        "sandbox",
        store,
        duration_s=1,
        client_concurrency=2,
        invoke=failing_invoke,
        sampler=fake_sampler,
    )

    assert store.rows
    assert all(r["status"] == "error" for r in store.rows)
    assert store.summary["errors"] == len(store.rows)
    # Sandbox summary reports exit-code distribution, not finding counts.
    assert "sandbox_exit" in store.summary


@pytest.mark.asyncio
async def test_run_guard_returns_existing_when_running(monkeypatch):
    """The start endpoint short-circuits to an in-flight run without dispatching."""
    import demos.firecracker_api as fc

    monkeypatch.setattr(
        fc, "_running_load_run", lambda: {"id": "existing-run", "workload": "semgrep"}
    )

    def _should_not_run(*_a, **_k):  # pragma: no cover - guard must short-circuit
        raise AssertionError("dispatch must not happen when a run is active")

    monkeypatch.setattr(fc, "_agent_is_busy", _should_not_run)
    monkeypatch.setattr(fc, "_insert_load_run", _should_not_run)

    result = await fc.start_load_test("semgrep")

    assert result == {"run_id": "existing-run", "already_running": True}


@pytest.mark.asyncio
async def test_run_guard_refuses_when_agent_busy(monkeypatch):
    """An active agent run makes the start endpoint refuse with HTTP 409."""
    import demos.firecracker_api as fc
    from fastapi import HTTPException

    monkeypatch.setattr(fc, "_running_load_run", lambda: None)
    monkeypatch.setattr(fc, "_agent_is_busy", lambda: True)

    with pytest.raises(HTTPException) as excinfo:
        await fc.start_load_test("sandbox")
    assert excinfo.value.status_code == 409
