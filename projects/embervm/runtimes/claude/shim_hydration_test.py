import io
import json
import os
import subprocess

import pytest

import shim


class _Adapter:
    def __init__(self, workspace):
        self.workspace = workspace
        self.calls = []

    def turn(self, *args, **kwargs):
        # Capture workspace AT CALL TIME (must be checkout_dir after hydration)
        self.calls.append({"args": args, "kwargs": kwargs, "workspace": self.workspace})
        return {"result": "ok"}


@pytest.fixture
def manager(tmp_path):
    instance = object.__new__(shim.ProcessManager)
    instance.workspace = str(tmp_path / "workspace")
    instance.claude = _Adapter(instance.workspace)
    instance.codex = _Adapter(instance.workspace)
    instance.pi = _Adapter(instance.workspace)
    instance._hydration_attempted = False
    instance._hydration_error = None
    instance._checkout_dir = None
    instance._hydration_status = None
    return instance


class _Headers:
    def __init__(self, length):
        self.length = length

    def get(self, name, default=None):
        return str(self.length) if name == "Content-Length" else default


def _post_payload(payload, manager):
    handler = object.__new__(shim.RequestHandler)
    raw = json.dumps(payload).encode("utf-8")
    handler.path = shim.TURN_PATH
    handler.headers = _Headers(len(raw))
    handler.rfile = io.BytesIO(raw)
    responses = []
    handler._send = lambda status, value: responses.append((status, value))
    handler.manager = manager
    shim.RequestHandler.do_POST(handler)
    return responses


def test_parse_repo_branch_from_payload(monkeypatch):
    class Manager:
        def __init__(self):
            self.calls = []

        def turn(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            return {"result": "ok"}

    for payload, expected in [
        (
            {"message": "hello", "repo": "owner/repo", "branch": "main"},
            ("owner/repo", "main"),
        ),
        ({"message": "hello"}, (None, None)),
    ]:
        manager = Manager()
        responses = _post_payload(payload, manager)
        assert responses == [(200, {"result": "ok"})]
        if expected == (None, None):
            assert manager.calls[0][1] == {}
        else:
            assert manager.calls[0][1] == {"repo": expected[0], "branch": expected[1]}

    for payload in (
        {"message": "hello", "repo": "owner/repo"},
        {"message": "hello", "branch": "main"},
    ):
        manager = Manager()
        responses = _post_payload(payload, manager)
        assert responses[0][0] == 400
        assert manager.calls == []


class _GitProcess:
    def __init__(self, returncode=0, stderr_text=""):
        self.returncode = returncode
        self.stdout = b""
        self.stderr = (
            stderr_text.encode() if isinstance(stderr_text, str) else stderr_text
        )


def test_hydration_runs_once_per_session(manager, monkeypatch):
    processes = [_GitProcess()]
    commands = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        return processes.pop(0)

    monkeypatch.setattr(shim.subprocess, "run", fake_run)
    manager.turn("first", repo="owner/repo", branch="main")
    manager.turn("second", repo="owner/repo", branch="main")

    assert len(commands) == 1
    assert manager._checkout_dir == os.path.join(manager.workspace, "src")


def test_hydration_skips_on_restored_volume(manager, monkeypatch):
    checkout_dir = os.path.join(manager.workspace, "src")
    os.makedirs(checkout_dir)
    monkeypatch.setattr(
        shim.subprocess, "run", lambda *_a, **_k: pytest.fail("git called")
    )

    manager.turn("first", repo="owner/repo", branch="main")

    assert manager._checkout_dir == checkout_dir
    assert manager.claude.workspace == checkout_dir


def test_hydration_failure_logs_and_continues(manager, monkeypatch, capsys):
    def fake_run(*_a, **_k):
        raise FileNotFoundError("git")

    monkeypatch.setattr(shim.subprocess, "run", fake_run)

    assert manager.turn("first", repo="owner/repo", branch="main") == {
        "result": "ok",
        "workspace_hydration": {"failed": "git"},
    }
    assert "workspace hydration failed for owner/repo@main" in capsys.readouterr().err


def test_hydration_timeout(manager, monkeypatch, capsys):
    def fake_run(*_a, **kwargs):
        if kwargs.get("timeout") == 120:
            raise subprocess.TimeoutExpired("git", 120)
        return _GitProcess()

    monkeypatch.setattr(shim.subprocess, "run", fake_run)

    manager.turn("first", repo="owner/repo", branch="main")

    assert "workspace hydration failed" in capsys.readouterr().err


def test_git_command_shape(manager, monkeypatch):
    processes = [_GitProcess()]
    commands = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        return processes.pop(0)

    monkeypatch.setattr(shim.subprocess, "run", fake_run)

    manager.turn("first", repo="owner/repo", branch="main")
    checkout_dir = os.path.join(manager.workspace, "src")
    assert commands == [
        [
            "git",
            "-c",
            "core.gitProxy=/tmp/ember-git-proxy",
            "clone",
            "--branch",
            "main",
            "--single-branch",
            "--filter=blob:none",
            "git://git-mirror.monolith.svc.cluster.local:9418/owner/repo",
            checkout_dir,
        ],
    ]


def test_cli_cwd_set_to_checkout(manager, monkeypatch):
    process = _GitProcess()
    monkeypatch.setattr(shim.subprocess, "run", lambda *_a, **_k: process)

    manager.turn("first", repo="owner/repo", branch="main")

    checkout_dir = os.path.join(manager.workspace, "src")
    assert manager.claude.workspace == checkout_dir
    assert manager.claude.calls[0]["workspace"] == checkout_dir


def test_no_hydration_when_repo_absent(manager, monkeypatch):
    monkeypatch.setattr(
        shim.subprocess, "run", lambda *_a, **_k: pytest.fail("git called")
    )

    manager.turn("first", session_id="session")

    assert manager._hydration_attempted is False
    assert manager.claude.calls


def test_hydration_works_for_non_default_branch(manager, monkeypatch):
    """Verify clone works for branches other than the mirror default."""
    processes = [_GitProcess()]
    commands = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        return processes.pop(0)

    monkeypatch.setattr(shim.subprocess, "run", fake_run)

    manager.turn("first", repo="owner/repo", branch="develop")

    checkout_dir = os.path.join(manager.workspace, "src")
    assert len(commands) == 1
    # Verify --branch is in command before clone executes checkout
    assert commands[0] == [
        "git",
        "-c",
        "core.gitProxy=/tmp/ember-git-proxy",
        "clone",
        "--branch",
        "develop",
        "--single-branch",
        "--filter=blob:none",
        "git://git-mirror.monolith.svc.cluster.local:9418/owner/repo",
        checkout_dir,
    ]
    assert manager._checkout_dir == checkout_dir


def test_hydration_updates_adapter_workspace(manager, monkeypatch):
    process = _GitProcess()
    monkeypatch.setattr(shim.subprocess, "run", lambda *_a, **_k: process)

    manager.turn("msg", repo="owner/repo", branch="main")

    assert manager.claude.calls[0]["workspace"] == os.path.join(
        manager.workspace, "src"
    )


def test_hydration_git_failure_128(manager, monkeypatch, capsys):
    """Git exit 128 (DNS failure, missing branch) is logged and degrades."""
    process = _GitProcess(returncode=128, stderr_text="fatal: repository not found")
    monkeypatch.setattr(shim.subprocess, "run", lambda *_a, **_k: process)

    result = manager.turn("first", repo="owner/repo", branch="nonexistent")

    assert result == {
        "result": "ok",
        "workspace_hydration": {
            "failed": "git command failed with exit code 128: fatal: repository not found"
        },
    }
    error = capsys.readouterr().err
    assert "fatal: repository not found" in error
    assert "workspace hydration failed for owner/repo@nonexistent" in error


def test_hydration_status_surfaced_in_turn(manager, monkeypatch):
    """Workspace hydration status included in turn record."""
    process = _GitProcess()
    monkeypatch.setattr(shim.subprocess, "run", lambda *_a, **_k: process)

    record = manager.turn("msg", repo="owner/repo", branch="main")
    assert record.get("workspace_hydration") == "ok"

    # A second request reuses the checkout created by the first hydration.
    os.makedirs(os.path.join(manager.workspace, "src"), exist_ok=True)
    record = manager.turn("msg2", repo="owner/repo", branch="main")
    assert record.get("workspace_hydration") == "skipped_existing"

    record = manager.turn("msg3")
    assert "workspace_hydration" not in record
