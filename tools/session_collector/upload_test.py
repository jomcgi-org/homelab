import json
import os
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from tools.cli.auth import read_cached_cf_token
from tools.session_collector.collector import run_collection
from tools.session_collector.state import load

ALLOW = {"jomcgi-org/homelab": "repo:jomcgi-org/homelab"}


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
                "role": "assistant",
                "model": "test-model",
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
