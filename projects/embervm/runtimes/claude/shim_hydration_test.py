import base64
import io
import json
import os
import subprocess
import threading

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
    instance._hydration_attempts = 0
    instance._hydration_error = None
    instance._checkout_dir = None
    instance._hydration_status = None
    instance._mount_lock = threading.Lock()
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
    processes = [_GitProcess(), _GitProcess()]
    commands = []

    def fake_run(command, **_kwargs):
        if command[1] == "clone":
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
    validation = _GitProcess()
    monkeypatch.setattr(shim.subprocess, "run", lambda *_a, **_k: validation)

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
        if kwargs.get("timeout") == shim.GIT_CLONE_TIMEOUT_SECONDS:
            raise subprocess.TimeoutExpired("git", shim.GIT_CLONE_TIMEOUT_SECONDS)
        return _GitProcess()

    monkeypatch.setattr(shim.subprocess, "run", fake_run)

    manager.turn("first", repo="owner/repo", branch="main")

    assert "workspace hydration failed" in capsys.readouterr().err


def test_failed_hydration_retries_on_next_turn_and_succeeds(
    manager, monkeypatch, capsys
):
    clone_calls = []

    def fake_run(command, **_kwargs):
        if command[1] == "clone":
            clone_calls.append(command)
            if len(clone_calls) == 1:
                raise subprocess.TimeoutExpired("git", shim.GIT_CLONE_TIMEOUT_SECONDS)
        return _GitProcess()

    monkeypatch.setattr(shim.subprocess, "run", fake_run)

    first = manager.turn("first", repo="owner/repo", branch="main")
    second = manager.turn("second", repo="owner/repo", branch="main")

    assert first["workspace_hydration"]["failed"]
    assert second["workspace_hydration"] == "ok"
    assert len(clone_calls) == 2
    assert manager.claude.workspace == os.path.join(manager.workspace, "src")
    assert "retrying workspace hydration" in capsys.readouterr().err


def test_hydration_failure_is_capped_and_keeps_reporting(manager, monkeypatch):
    clone_calls = []

    def fake_run(command, **_kwargs):
        if command[1] == "clone":
            clone_calls.append(command)
            raise subprocess.TimeoutExpired("git", shim.GIT_CLONE_TIMEOUT_SECONDS)
        return _GitProcess()

    monkeypatch.setattr(shim.subprocess, "run", fake_run)

    records = [
        manager.turn("turn", repo="owner/repo", branch="main")
        for _ in range(shim.HYDRATION_ATTEMPT_CAP + 1)
    ]

    assert len(clone_calls) == shim.HYDRATION_ATTEMPT_CAP
    assert manager._hydration_attempts == shim.HYDRATION_ATTEMPT_CAP
    assert all(record["workspace_hydration"]["failed"] for record in records)


def test_git_command_shape(manager, monkeypatch):
    processes = [_GitProcess(), _GitProcess()]
    commands = []

    def fake_run(command, **_kwargs):
        if command[1] == "clone":
            commands.append(command)
        return processes.pop(0)

    monkeypatch.setattr(shim.subprocess, "run", fake_run)

    manager.turn("first", repo="owner/repo", branch="main")
    checkout_dir = os.path.join(manager.workspace, "src")
    assert commands == [
        [
            "git",
            "clone",
            "--progress",
            "--branch",
            "main",
            "--config",
            "http.proxy=http://127.0.0.1:1024",
            "--config",
            "http.https://github.com/.extraHeader=Authorization: Basic %s"
            % shim._github_basic_optin(),
            "--single-branch",
            "--filter=blob:none",
            "https://github.com/owner/repo.git",
            checkout_dir,
        ],
    ]


def test_git_clone_regression_guards(manager, monkeypatch):
    """Verify critical clone command properties to prevent regressions."""
    processes = [_GitProcess(), _GitProcess()]
    commands = []

    def fake_run(command, **_kwargs):
        if command[1] == "clone":
            commands.append(command)
        return processes.pop(0)

    monkeypatch.setattr(shim.subprocess, "run", fake_run)

    manager.turn("first", repo="owner/repo", branch="main")

    assert len(commands) == 1
    command = commands[0]

    # Regression guard: --depth=1 must be ABSENT. It was the workaround for
    # #4417 while hydration ran over git:// (:9418, the one port whose tunnels
    # deliberately never close). Over https the tunnel closes normally, and the
    # whole point of the move is that history is present.
    assert "--depth=1" not in command, "clone command must not be shallow"

    # Regression guard: the blob filter is what makes full history affordable.
    # Dropping it turns hydration into a full-content clone of all history.
    assert "--filter=blob:none" in command, "clone command missing --filter=blob:none"

    # Regression guard: https to GitHub, never git://. A git:// URL would put
    # hydration back on the port whose lingering tunnels wedge connection #2,
    # and would also lose the sidecar's credential injection (it can only set a
    # header on bytes it can read, which requires the TLS-MITM lane).
    url = command[-2]
    assert url.startswith("https://github.com/"), "clone must use https to GitHub"
    assert not any(arg.startswith("git://") for arg in command), (
        "clone command must not use the git:// transport"
    )

    # Verify load-bearing flags remain. http.proxy, not core.gitProxy: the
    # helper is a git:// proxy and does nothing for an https remote, and this
    # subprocess does not inherit the CLI spawn env that carries HTTPS_PROXY.
    assert "--single-branch" in command, "clone command missing --single-branch"
    assert any(arg.startswith("http.proxy=") for arg in command), (
        "clone command missing http.proxy config"
    )


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

    assert manager._hydration_attempts == 0
    assert manager.claude.calls


def test_hydration_works_for_non_default_branch(manager, monkeypatch):
    """Verify clone works for branches other than the repository default."""
    processes = [_GitProcess(), _GitProcess()]
    commands = []

    def fake_run(command, **_kwargs):
        if command[1] == "clone":
            commands.append(command)
        return processes.pop(0)

    monkeypatch.setattr(shim.subprocess, "run", fake_run)

    manager.turn("first", repo="owner/repo", branch="develop")

    checkout_dir = os.path.join(manager.workspace, "src")
    assert len(commands) == 1
    # Verify --branch is in command before clone executes checkout
    assert commands[0] == [
        "git",
        "clone",
        "--progress",
        "--branch",
        "develop",
        "--config",
        "http.proxy=http://127.0.0.1:1024",
        "--config",
        "http.https://github.com/.extraHeader=Authorization: Basic %s"
        % shim._github_basic_optin(),
        "--single-branch",
        "--filter=blob:none",
        "https://github.com/owner/repo.git",
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


def test_hydration_timing_reports_clone_and_existing_status(
    manager, monkeypatch, capsys
):
    checkout_dir = os.path.join(manager.workspace, "src")

    def fake_run(command, **_kwargs):
        if command[1] == "clone":
            os.makedirs(os.path.join(checkout_dir, ".git"), exist_ok=True)
        return _GitProcess()

    monkeypatch.setattr(shim.subprocess, "run", fake_run)
    manager.turn("first", repo="owner/repo", branch="main")
    first_lines = capsys.readouterr().err.splitlines()
    assert any(
        line.startswith(
            "ember-claude-shim: turn-timing phase=hydration status=cloned ms="
        )
        and line.rsplit("ms=", 1)[1].isdigit()
        for line in first_lines
    )
    assert any(
        line.startswith("ember-claude-shim: turn-timing phase=hydration_clone ms=")
        and line.rsplit("ms=", 1)[1].isdigit()
        for line in first_lines
    )
    assert any(
        line.startswith("ember-claude-shim: turn-timing phase=total ms=")
        and line.rsplit("ms=", 1)[1].isdigit()
        for line in first_lines
    )

    manager.turn("second", repo="owner/repo", branch="main")
    second_lines = capsys.readouterr().err.splitlines()
    skipped = [
        line
        for line in second_lines
        if line.startswith(
            "ember-claude-shim: turn-timing phase=hydration status=skipped_existing ms="
        )
    ]
    assert len(skipped) == 1
    assert skipped[0].rsplit("ms=", 1)[1].isdigit()
    assert (
        sum(
            line.startswith("ember-claude-shim: turn-timing phase=total ms=")
            and line.rsplit("ms=", 1)[1].isdigit()
            for line in second_lines
        )
        == 1
    )


def test_failed_clone_leaves_no_directory(manager, monkeypatch):
    checkout_dir = os.path.join(manager.workspace, "src")

    def fake_run(command, **_kwargs):
        if command[1] == "clone":
            os.makedirs(checkout_dir, exist_ok=True)
            return _GitProcess(returncode=1, stderr_text="clone failed")
        pytest.fail("unexpected validation command")

    monkeypatch.setattr(shim.subprocess, "run", fake_run)
    manager.turn("first", repo="owner/repo", branch="main")
    assert not os.path.exists(checkout_dir)


def test_poisoned_directory_is_cleaned_and_recloned(manager, monkeypatch):
    checkout_dir = os.path.join(manager.workspace, "src")
    os.makedirs(checkout_dir)
    calls = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        if command[1] == "clone":
            os.makedirs(os.path.join(checkout_dir, ".git"), exist_ok=True)
            return _GitProcess()
        if command[1:3] == ["-C", checkout_dir]:
            return _GitProcess(returncode=1 if len(calls) == 1 else 0)
        pytest.fail("unexpected git command")

    monkeypatch.setattr(shim.subprocess, "run", fake_run)
    manager.turn("first", repo="owner/repo", branch="main")
    assert [command[1] for command in calls] == ["-C", "clone", "-C"]


def test_cloning_progress_is_pushed_before_the_clone(manager, monkeypatch):
    """The console shows the clone WHILE it runs, so the push must precede it.

    Pushed after, it would only ever be visible once the thing it announces had
    already finished, which is the failure this guards: the whole point is that
    a multi-second hydration stops reading as dead time under the console's
    "starting the agent..." fallback.
    """
    events = []

    class _CapturingPusher:
        def __init__(self, progress_token):
            events.append(("pusher", progress_token))

        def push(self, text, activities=None):
            events.append(("push", text, activities))

    monkeypatch.setattr(shim, "_ProgressPusher", _CapturingPusher)

    def fake_run(command, **_kwargs):
        if command[1] == "clone":
            events.append(("clone", command[-2]))
        return _GitProcess()

    monkeypatch.setattr(shim.subprocess, "run", fake_run)

    manager.turn("first", repo="owner/repo", branch="main", progress_token="tok-1")

    assert ("pusher", "tok-1") in events
    push_index = next(i for i, e in enumerate(events) if e[0] == "push")
    clone_index = next(i for i, e in enumerate(events) if e[0] == "clone")
    assert push_index < clone_index, "progress must be pushed BEFORE the clone runs"

    # A bare string is a valid activity to the console (activityParts treats
    # `typeof activity === "string"` as {verb, detail: ""}), and naming the repo
    # and branch is what distinguishes this from a generic spinner.
    assert events[push_index][2] == ["cloning owner/repo@main"]


def test_no_progress_push_without_a_token(manager, monkeypatch):
    """No token means no ingest route, so pushing would be a wasted round trip."""
    pushes = []

    class _CapturingPusher:
        def __init__(self, progress_token):
            pushes.append(progress_token)

        def push(self, *_args, **_kwargs):
            pushes.append("pushed")

    monkeypatch.setattr(shim, "_ProgressPusher", _CapturingPusher)
    monkeypatch.setattr(shim.subprocess, "run", lambda *_a, **_k: _GitProcess())

    manager.turn("first", repo="owner/repo", branch="main")

    assert pushes == []


def test_clone_sends_an_authorization_header_to_opt_into_injection(
    manager, monkeypatch
):
    """Regression for a live 401: the clone MUST present Authorization.

    The egress sidecar's injection is presence-keyed. It replaces a header the
    guest already sent and discards the value (injectRequest: `requested :=
    len(req.Header.Values(sec.Header)) > 0 || injectAlwaysPath(...)` then
    `if !requested { return false }`). Sending nothing injects nothing, so
    github.com answered 401 and git reported "could not read Username for
    'https://github.com'". Seen in prod as
    `"egress injected" injected=false status=401`.

    injectAlwaysPaths cannot substitute: it is an exact match on req.URL.Path and
    git's paths are per repository, so it would have to enumerate every repo.
    """
    monkeypatch.setenv("GH_TOKEN", "dummy-from-guest-env")
    commands = []

    def fake_run(command, **_kwargs):
        if command[1] == "clone":
            commands.append(command)
        return _GitProcess()

    monkeypatch.setattr(shim.subprocess, "run", fake_run)
    manager.turn("first", repo="owner/repo", branch="main")

    configs = [
        arg for arg in commands[0] if arg.startswith("http.") and "extraHeader" in arg
    ]
    assert len(configs) == 1, "clone must set exactly one extraHeader"
    config = configs[0]

    # Scoped by URL, so the opt-in is never presented to any other host.
    assert config.startswith("http.https://github.com/.extraHeader="), (
        "the Authorization opt-in must be scoped to github.com"
    )

    # Derived from guest env, NOT a literal in shim.py. A checked-in base64 blob
    # reads as a credential and would drift from the dummy gh actually presents.
    encoded = config.split("Authorization: Basic ", 1)[1]
    assert base64.b64decode(encoded).decode() == (
        "x-access-token:dummy-from-guest-env"
    ), "the opt-in must be built from GH_TOKEN in the guest environment"


def test_github_optin_is_well_formed_even_without_gh_token(monkeypatch):
    """An absent GH_TOKEN must still yield a header, because presence is all.

    Dropping the header on a missing env var would trade a loud config problem
    for a silent 401 and an empty workspace, which is the failure mode this
    whole path already proved is easy to miss.
    """
    monkeypatch.delenv("GH_TOKEN", raising=False)
    encoded = shim._github_basic_optin()
    assert base64.b64decode(encoded).decode() == "x-access-token:"
