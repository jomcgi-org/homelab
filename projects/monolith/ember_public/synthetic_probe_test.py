from datetime import datetime, timezone

import pytest

import ember_public.synthetic_probe as probe
from ember_public.synthetic_models import EmberSyntheticProbe


@pytest.mark.asyncio
async def test_bazel_drift_is_not_ok(monkeypatch):
    async def run_query(_):
        return 200, {"analyzed_line": "1 package loaded", "wall_ms": 4}

    monkeypatch.setattr(probe.bazel_core, "run_query", run_query)
    result = await probe.probe_bazel()
    assert result["ok"] is False
    assert "0 packages loaded" in result["detail"]


@pytest.mark.asyncio
async def test_semgrep_empty_findings_is_not_ok(monkeypatch):
    async def scan_files(*_, **__):
        return {"findings": []}

    monkeypatch.setattr("semgrep_scan.client.scan_files", scan_files)
    result = await probe.probe_semgrep()
    assert result["ok"] is False
    assert "no findings" in result["detail"]


@pytest.mark.asyncio
async def test_postgres_busy_is_skipped(monkeypatch):
    monkeypatch.setattr(probe.core, "demo_pg_dsn", lambda: "postgres://test")
    monkeypatch.setattr(probe.core, "try_acquire_query_slot", lambda: False)
    result = await probe.probe_postgres()
    assert result["skip"] is True


@pytest.mark.asyncio
async def test_postgres_aggregate_roundtrip_success(monkeypatch):
    """Successful aggregate roundtrip records ok with connect_ms as latency."""
    monkeypatch.setattr(probe.core, "demo_pg_dsn", lambda: "postgres://test")
    monkeypatch.setattr(probe.core, "EMBERVM_URL", "http://test")
    monkeypatch.setattr(
        probe.core,
        "cached_demo_pg_status",
        lambda: {"state": "asleep", "generation": 1},
    )
    monkeypatch.setattr(probe.core, "try_acquire_query_slot", lambda: True)
    monkeypatch.setattr(
        probe.core,
        "demo_pg_orders_roundtrip",
        lambda *_: {"connect_ms": 42, "total_ms": 100},
    )
    monkeypatch.setattr(probe.core, "record_query_outcome", lambda **_: None)
    monkeypatch.setattr(probe.core, "classify_wake", lambda _: "cold")
    monkeypatch.setattr(probe.core, "release_query_slot", lambda: None)

    result = await probe.probe_postgres()
    assert result["ok"] is True
    assert result["latency_ms"] == 42
    assert "cold" in result["detail"]


@pytest.mark.asyncio
async def test_page_5xx_retried_once_then_failed(monkeypatch):
    class Response:
        status_code = 503
        text = "unavailable"

    class Client:
        calls = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, *args):
            self.calls += 1
            return Response()

    client = Client()
    monkeypatch.setenv("EMBER_SYNTHETIC_BASE_URL", "https://example.test")
    monkeypatch.setattr(probe.httpx, "AsyncClient", lambda **_: client)
    monkeypatch.setattr(probe.asyncio, "sleep", lambda *_: _done())
    result = await probe.probe_pages()
    assert client.calls == 2
    assert result["ok"] is False
    assert "/ember" in result["detail"]


async def _done():
    return None


@pytest.mark.asyncio
async def test_record_preserves_last_ok_at_on_failure(monkeypatch):
    class FakeSession:
        row = EmberSyntheticProbe(
            demo="bazel",
            ok=True,
            detail="old",
            checked_at=datetime.now(timezone.utc),
            last_ok_at=datetime(2026, 7, 27, tzinfo=timezone.utc),
        )

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def get(self, *_):
            return self.row

        def commit(self):
            return None

    session = FakeSession()
    monkeypatch.setattr(probe, "Session", lambda *_: session)
    old = session.row.last_ok_at
    await probe.record("bazel", {"ok": False, "detail": "failed", "latency_ms": None})
    assert session.row.ok is False
    assert session.row.last_ok_at == old
