import json
import os
import subprocess
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest

from tools.cli.auth import read_cached_cf_token
from tools.session_collector.collector import (
    _payload,
    run_collection,
    run_usage_backfill,
)
from tools.session_collector.models import Session
from tools.session_collector.render import render
from tools.session_collector.scope import discover_repo
from tools.session_collector.state import load, save
from tools.session_collector.upload import UploadResult
from tools.session_collector.upload import upload_usage

ALLOW = {"jomcgi-org/homelab": "repo:jomcgi-org/homelab"}


@pytest.fixture(autouse=True)
def _register_test_worktree(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "tools.session_collector.scope.homelab_worktrees",
        lambda: frozenset({tmp_path}),
    )


def _session(claude_dir: Path, name: str, cwd: str) -> Path:
    project = claude_dir / "project"
    project.mkdir(parents=True, exist_ok=True)
    path = project / f"{name}.jsonl"
    records = [
        {
            "type": "user",
            "sessionId": name,
            "cwd": cwd,
            "timestamp": "2026-01-01T00:00:00Z",
            "message": {"role": "user", "content": "x" * 3000},
        },
        {
            "type": "assistant",
            "sessionId": name,
            "cwd": cwd,
            "timestamp": "2026-01-01T00:01:00Z",
            "message": {
                "id": f"{name}-assistant",
                "role": "assistant",
                "model": "test-model",
                "usage": {
                    "input_tokens": 100,
                    "cache_read_input_tokens": 30,
                    "cache_creation_input_tokens": 40,
                    "output_tokens": 20,
                },
                "content": [{"type": "text", "text": "done"}],
            },
        },
    ]
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n")
    os.utime(path, (100, 100))
    return path


def _run(tmp_path, transport, **overrides):
    claude_dir = tmp_path / "claude"
    codex_dir = tmp_path / "codex"
    codex_dir.mkdir(exist_ok=True)
    options = {
        "claude_dir": claude_dir,
        "codex_dir": codex_dir,
        "state_file": tmp_path / "state.json",
        "allowlist": ALLOW,
        "path_allowlist": {tmp_path: "jomcgi-org/homelab"},
        "quiet_minutes": 0,
        "max_uploads": 20,
        "client": httpx.Client(transport=httpx.MockTransport(transport)),
        "token_reader": lambda hostname: "cached-token",
        "now": 1000,
    }
    options.update(overrides)
    return claude_dir, options


@pytest.mark.parametrize("created", [True, False])
def test_201_created_values_are_uploaded(tmp_path, created):
    requests = []

    def transport(request):
        requests.append(request)
        return httpx.Response(
            201, json={"raw_id": f"raw-{created}", "created": created}
        )

    claude_dir, options = _run(tmp_path, transport)
    transcript = _session(claude_dir, "one", str(tmp_path / "homelab"))
    assert run_collection(**options) == 0
    entry = load(options["state_file"])[str(transcript.resolve())]
    assert entry["status"] == "uploaded"
    assert entry["raw_id"] == f"raw-{created}"
    payload = json.loads(requests[0].content)
    assert payload["source"] == "claude-session"
    assert payload["extra"]["bytes_original"] == transcript.stat().st_size
    assert payload["extra"]["usage"] == {
        "input_tokens": 100,
        "output_tokens": 20,
        "cache_read_tokens": 30,
        "cache_write_tokens": 40,
        "reasoning_tokens": 0,
        "messages": 1,
        "shape": "claude",
    }
    assert payload["extra"]["models"] == ["test-model"]


def test_payload_caps_models_at_twenty():
    session = Session(
        provider="claude",
        session_id="id",
        cwd="/tmp/homelab",
        git_branch=None,
        model=None,
        started_at="start",
        ended_at="end",
        title="title",
        records_total=0,
        records_kept=0,
        collector_version="claude-v2",
        turns=[],
        usage=None,
        models=[f"model-{index:02d}" for index in range(25)],
    )
    rendered = render(session, "jomcgi-org/homelab", "repo:jomcgi-org/homelab")
    payload = _payload(session, rendered, 0)
    assert payload["extra"]["models"] == session.models[:20]


def test_usage_backfill_iterates_uploaded_state_and_retries(tmp_path, capsys):
    claude_dir = tmp_path / "claude"
    transcript = _session(claude_dir, "old", str(tmp_path / "homelab"))
    missing = tmp_path / "missing.jsonl"
    state_file = tmp_path / "state.json"
    save(
        state_file,
        {
            str(transcript.resolve()): {"status": "uploaded", "raw_id": "raw-old"},
            str(missing): {"status": "uploaded", "raw_id": "raw-missing"},
            "failed": {"status": "failed", "raw_id": None},
        },
    )
    results = iter(
        [
            UploadResult("failed", status_code=503),
            UploadResult("uploaded", "raw-old", status_code=200),
            UploadResult("uploaded", "raw-old", status_code=200),
        ]
    )

    with patch(
        "tools.session_collector.collector.upload_usage",
        side_effect=lambda *args, **kwargs: next(results),
    ) as uploader:
        assert (
            run_usage_backfill(
                state_file=state_file,
                base_url="http://monolith.example.ts.net",
                auth="none",
                client=httpx.Client(transport=httpx.MockTransport(lambda r: None)),
            )
            == 0
        )
        assert "usage_sent_at" not in load(state_file)[str(transcript.resolve())]

        assert (
            run_usage_backfill(
                state_file=state_file,
                base_url="http://monolith.example.ts.net",
                auth="none",
                client=httpx.Client(transport=httpx.MockTransport(lambda r: None)),
            )
            == 0
        )
        sent_at = load(state_file)[str(transcript.resolve())]["usage_sent_at"]

        assert (
            run_usage_backfill(
                state_file=state_file,
                base_url="http://monolith.example.ts.net",
                auth="none",
                force=True,
                client=httpx.Client(transport=httpx.MockTransport(lambda r: None)),
            )
            == 0
        )

    assert uploader.call_count == 3
    payload = uploader.call_args_list[0].args[4]
    assert payload["usage"]["shape"] == "claude"
    assert payload["models"] == ["test-model"]
    assert payload["model"] == "test-model"
    assert load(state_file)[str(transcript.resolve())]["usage_sent_at"] >= sent_at
    output = capsys.readouterr().out
    assert "failed" in output
    assert "missing" in output
    assert "already sent" not in output
    assert "summary sent=1 already_sent=0 missing=1 failed=0" in output


def test_usage_backfill_releases_state_lock_for_bounded_batches(tmp_path):
    claude_dir = tmp_path / "claude"
    first = _session(claude_dir, "first", str(tmp_path / "homelab"))
    second = _session(claude_dir, "second", str(tmp_path / "homelab"))
    state_file = tmp_path / "state.json"
    save(
        state_file,
        {
            str(first.resolve()): {"status": "uploaded", "raw_id": "raw-first"},
            str(second.resolve()): {"status": "uploaded", "raw_id": "raw-second"},
        },
    )
    lock_active = False
    lock_entries = 0

    @contextmanager
    def observed_lock(_state_file):
        nonlocal lock_active, lock_entries
        assert lock_active is False
        lock_active = True
        lock_entries += 1
        try:
            yield
        finally:
            lock_active = False

    def upload_outside_lock(*_args, **_kwargs):
        assert lock_active is False
        return UploadResult("uploaded", status_code=200)

    with (
        patch("tools.session_collector.collector.locked", observed_lock),
        patch(
            "tools.session_collector.collector.upload_usage",
            side_effect=upload_outside_lock,
        ) as uploader,
    ):
        assert (
            run_usage_backfill(
                state_file=state_file,
                base_url="http://monolith.example.ts.net",
                auth="none",
                limit=1,
                client=httpx.Client(
                    transport=httpx.MockTransport(lambda request: None)
                ),
            )
            == 0
        )

    assert uploader.call_count == 2
    assert lock_entries == 5


def test_upload_usage_uses_raw_endpoint_and_cloudflare_cookie():
    requests = []

    def transport(request):
        requests.append(request)
        return httpx.Response(200, json={"raw_id": "raw-one", "updated": True})

    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        result = upload_usage(
            client,
            "https://private.example",
            "cached-token",
            "raw-one",
            {"usage": {}, "models": []},
            cloudflare=True,
        )

    assert result.status == "uploaded"
    assert requests[0].url.path == "/api/knowledge/raws/raw-one/usage"
    assert requests[0].headers["cookie"] == "CF_Authorization=cached-token"


def test_302_stops_without_further_uploads(tmp_path):
    calls = 0

    def transport(request):
        nonlocal calls
        calls += 1
        return httpx.Response(302, headers={"location": "/login"})

    claude_dir, options = _run(tmp_path, transport)
    _session(claude_dir, "one", str(tmp_path / "homelab"))
    _session(claude_dir, "two", str(tmp_path / "homelab"))
    assert run_collection(**options) == 0
    assert calls == 1
    assert load(options["state_file"]) == {}


def test_none_auth_sends_no_cookie_header(tmp_path):
    requests = []

    def transport(request):
        requests.append(request)
        return httpx.Response(201, json={"raw_id": "raw-one", "created": True})

    claude_dir, options = _run(
        tmp_path,
        transport,
        auth="none",
        base_url="http://monolith.example.ts.net",
        token_reader=lambda hostname: pytest.fail("token reader must not be called"),
    )
    _session(claude_dir, "one", str(tmp_path / "homelab"))
    assert run_collection(**options) == 0
    assert "Cookie" not in requests[0].headers


@pytest.mark.parametrize("status_code", [401, 403])
def test_none_auth_marks_unauthorized_as_failed(tmp_path, status_code):
    def transport(request):
        return httpx.Response(status_code)

    claude_dir, options = _run(tmp_path, transport, auth="none")
    transcript = _session(claude_dir, "one", str(tmp_path / "homelab"))
    assert run_collection(**options) == 0
    entry = load(options["state_file"])[str(transcript.resolve())]
    assert entry["status"] == "failed"
    assert entry["reason"] == "unauthorized"


@pytest.mark.parametrize(
    "error_type",
    [httpx.ConnectError, httpx.ConnectTimeout, httpx.RemoteProtocolError],
)
def test_none_auth_unreachable_stops_without_state_and_retries(
    tmp_path, capsys, error_type
):
    calls = 0

    def transport(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise error_type("tailnet down", request=request)
        return httpx.Response(201, json={"raw_id": "raw", "created": True})

    claude_dir, options = _run(
        tmp_path,
        transport,
        auth="none",
        base_url="http://monolith.example.ts.net",
    )
    one = _session(claude_dir, "one", str(tmp_path / "homelab"))
    two = _session(claude_dir, "two", str(tmp_path / "homelab"))
    assert run_collection(**options) == 0
    assert calls == 1
    assert load(options["state_file"]) == {}
    assert not options["state_file"].exists()
    assert capsys.readouterr().err == "tailnet unreachable: monolith.example.ts.net\n"

    assert run_collection(**options) == 0
    state = load(options["state_file"])
    assert state[str(one.resolve())]["status"] == "uploaded"
    assert state[str(one.resolve())]["failures"] == 0
    assert state[str(two.resolve())]["status"] == "uploaded"
    assert state[str(two.resolve())]["failures"] == 0


def test_500_marks_failed_and_continues(tmp_path):
    calls = 0

    def transport(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(500)
        return httpx.Response(201, json={"raw_id": "raw-two", "created": True})

    claude_dir, options = _run(tmp_path, transport)
    one = _session(claude_dir, "one", str(tmp_path / "homelab"))
    two = _session(claude_dir, "two", str(tmp_path / "homelab"))
    assert run_collection(**options) == 0
    state = load(options["state_file"])
    assert state[str(one.resolve())]["status"] == "failed"
    assert state[str(one.resolve())]["reason"] == "HTTP 500"
    assert state[str(two.resolve())]["status"] == "uploaded"


def test_max_uploads_is_honored(tmp_path):
    calls = 0

    def transport(request):
        nonlocal calls
        calls += 1
        return httpx.Response(201, json={"raw_id": "raw", "created": True})

    claude_dir, options = _run(tmp_path, transport, max_uploads=1)
    _session(claude_dir, "one", str(tmp_path / "homelab"))
    _session(claude_dir, "two", str(tmp_path / "homelab"))
    assert run_collection(**options) == 0
    assert calls == 1


def test_missing_cached_token_never_calls_cloudflared(tmp_path):
    def transport(request):
        raise AssertionError("upload must not be attempted")

    claude_dir, options = _run(tmp_path, transport)
    _session(claude_dir, "one", str(tmp_path / "homelab"))
    token_dir = tmp_path / "tokens"
    token_dir.mkdir()
    with (
        patch("tools.cli.auth.CF_TOKEN_DIR", token_dir),
        patch("tools.cli.auth.subprocess.run") as login,
    ):
        options["token_reader"] = read_cached_cf_token
        assert run_collection(**options) == 0
    login.assert_not_called()


def test_outside_allowlist_is_recorded_as_skipped(tmp_path):
    def transport(request):
        raise AssertionError("upload must not be attempted")

    claude_dir, options = _run(tmp_path, transport, allowlist={})
    transcript = _session(claude_dir, "one", str(tmp_path / "other"))
    assert run_collection(**options) == 0
    entry = load(options["state_file"])[str(transcript.resolve())]
    assert entry["status"] == "skipped"
    assert entry["reason"] == "outside allowlist"


def test_deleted_discovered_file_does_not_abort_other_uploads(tmp_path, monkeypatch):
    calls = 0

    def transport(request):
        nonlocal calls
        calls += 1
        return httpx.Response(201, json={"raw_id": "raw", "created": True})

    claude_dir, options = _run(tmp_path, transport)
    deleted = _session(claude_dir, "one", str(tmp_path / "homelab"))
    uploaded = _session(claude_dir, "two", str(tmp_path / "homelab"))

    def discover_then_delete(*args):
        deleted.unlink()
        return [deleted.resolve(), uploaded.resolve()]

    monkeypatch.setattr(
        "tools.session_collector.collector.discover", discover_then_delete
    )
    assert run_collection(**options) == 0
    assert calls == 1
    assert str(deleted.resolve()) not in load(options["state_file"])
    assert load(options["state_file"])[str(uploaded.resolve())]["status"] == "uploaded"


def test_three_failures_park_sessions_and_release_budget(tmp_path):
    attempts = []

    def transport(request):
        attempts.append(json.loads(request.content)["original_url"])
        return httpx.Response(500)

    claude_dir, options = _run(tmp_path, transport, max_uploads=2)
    one = _session(claude_dir, "a", str(tmp_path / "homelab"))
    two = _session(claude_dir, "b", str(tmp_path / "homelab"))
    three = _session(claude_dir, "c", str(tmp_path / "homelab"))
    for _ in range(4):
        assert run_collection(**options) == 0
    state = load(options["state_file"])
    assert state[str(one.resolve())]["failures"] == 3
    assert state[str(two.resolve())]["failures"] == 3
    assert state[str(three.resolve())]["failures"] == 1
    assert attempts[-1] == "claude-session:c"


def test_git_probe_timeout_is_failed_and_collection_continues(tmp_path, monkeypatch):
    calls = 0

    def transport(request):
        nonlocal calls
        calls += 1
        return httpx.Response(201, json={"raw_id": "raw-two", "created": True})

    claude_dir, options = _run(tmp_path, transport, path_allowlist={})
    first_repo = tmp_path / "first-repo"
    second_repo = tmp_path / "second-repo"
    first_repo.mkdir()
    second_repo.mkdir()
    first = _session(claude_dir, "one", str(first_repo))
    second = _session(claude_dir, "two", str(second_repo))
    probes = 0

    def git_probe(*args, **kwargs):
        nonlocal probes
        probes += 1
        if probes == 1:
            raise subprocess.TimeoutExpired("git", 10)
        return SimpleNamespace(
            returncode=0, stdout="git@github.com:jomcgi-org/homelab.git\n"
        )

    monkeypatch.setattr("tools.session_collector.scope.subprocess.run", git_probe)
    assert run_collection(**options) == 0
    state = load(options["state_file"])
    assert state[str(first.resolve())]["status"] == "failed"
    assert state[str(first.resolve())]["reason"] == "TimeoutExpired"
    assert state[str(second.resolve())]["status"] == "uploaded"
    assert calls == 1


def test_unexpected_session_error_is_recorded_and_collection_continues(
    tmp_path, monkeypatch
):
    calls = 0

    def transport(request):
        nonlocal calls
        calls += 1
        return httpx.Response(201, json={"raw_id": "raw-two", "created": True})

    claude_dir, options = _run(tmp_path, transport)
    cwd = str(tmp_path / "homelab")
    first = _session(claude_dir, "one", cwd)
    second = _session(claude_dir, "two", cwd)
    from tools.session_collector import collector

    original_render = collector.render
    renders = 0

    def fail_once(*args, **kwargs):
        nonlocal renders
        renders += 1
        if renders == 1:
            raise RuntimeError("planted value must not abort collection")
        return original_render(*args, **kwargs)

    monkeypatch.setattr(collector, "render", fail_once)
    assert run_collection(**options) == 0
    state = load(options["state_file"])
    assert state[str(first.resolve())]["status"] == "failed"
    assert state[str(first.resolve())]["reason"] == "RuntimeError"
    assert state[str(first.resolve())]["cwd"] == cwd
    assert state[str(first.resolve())]["repo"] == "jomcgi-org/homelab"
    assert discover_repo(cwd, state, {}) == "jomcgi-org/homelab"
    assert state[str(second.resolve())]["status"] == "uploaded"
    assert calls == 1
