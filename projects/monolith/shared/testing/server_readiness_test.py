"""Readiness retries preserve the existing deadline and require a healthy server."""

from types import SimpleNamespace

import httpx
import pytest

from shared.testing import plugin


@pytest.fixture
def clock(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(plugin.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(
        plugin.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds)
    )
    return now


@pytest.mark.parametrize(
    "error", [httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout]
)
def test_transient_probe_failure_can_recover(monkeypatch, clock, error):
    calls = []

    def probe(url, timeout):
        calls.append((url, timeout))
        if len(calls) == 1:
            raise error("server still starting")
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr(plugin.httpx, "get", probe)
    plugin._wait_for_live_server("http://127.0.0.1:1234")
    assert len(calls) == 2
    assert all(url.endswith("/healthz") and timeout <= 1 for url, timeout in calls)


@pytest.mark.parametrize("failure", ["timeout", "unhealthy"])
def test_failed_probes_cannot_extend_startup_deadline(monkeypatch, clock, failure):
    calls = []

    def probe(url, timeout):
        calls.append(timeout)
        clock[0] += timeout
        if failure == "timeout":
            raise httpx.ReadTimeout("not ready")
        return SimpleNamespace(status_code=503)

    monkeypatch.setattr(plugin.httpx, "get", probe)
    with pytest.raises(RuntimeError, match="failed to start within 10s"):
        plugin._wait_for_live_server("http://127.0.0.1:1234")
    assert clock[0] == pytest.approx(10.0)
    assert calls[-1] < 1.0
    assert len(calls) <= 10


def test_unexpected_probe_error_is_not_hidden(monkeypatch, clock):
    def probe(url, timeout):
        raise ValueError("bad configuration")

    monkeypatch.setattr(plugin.httpx, "get", probe)
    with pytest.raises(ValueError, match="bad configuration"):
        plugin._wait_for_live_server("http://127.0.0.1:1234")


@pytest.mark.parametrize("fail_startup", [False, True])
def test_server_cleanup_runs_after_startup_failure_or_fixture_close(
    monkeypatch, fail_startup
):
    import sys
    from unittest.mock import Mock

    from core import db

    server = SimpleNamespace(run=Mock(), should_exit=False)
    thread = SimpleNamespace(start=Mock(), join=Mock())
    engine = Mock()
    monkeypatch.setattr(db, "get_engine", engine)
    monkeypatch.setattr(plugin, "_find_free_port", lambda: 1234)
    monkeypatch.setattr(plugin.threading, "Thread", lambda **kwargs: thread)
    monkeypatch.setitem(
        sys.modules, "uvicorn", SimpleNamespace(Config=Mock(), Server=lambda _: server)
    )
    monkeypatch.setitem(sys.modules, "app.main", SimpleNamespace(app=object()))
    for name in ("DATABASE_URL", "STATIC_DIR", "DISCORD_BOT_TOKEN", "ICAL_FEED_URL"):
        monkeypatch.setenv(name, "test-placeholder")

    def ready(url):
        if fail_startup:
            raise RuntimeError("startup failed")

    monkeypatch.setattr(plugin, "_wait_for_live_server", ready)
    fixture = plugin.live_server.__wrapped__(
        SimpleNamespace(url="postgresql+psycopg://local/test")
    )
    if fail_startup:
        with pytest.raises(RuntimeError, match="startup failed"):
            next(fixture)
    else:
        assert next(fixture) == "http://127.0.0.1:1234"
        assert server.should_exit is False
        fixture.close()
    assert server.should_exit is True
    thread.join.assert_called_once_with(timeout=5)
    assert engine.cache_clear.call_count == 2
