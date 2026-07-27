"""Unit tests for the Claude guest shim using a fake stream-json CLI."""

import json
import ast
import os
import signal
import threading
import time
import subprocess
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

import pytest

import shim


FAKE_CLI = r"""#!/usr/bin/env python3
import json
import os
import signal
import sys

args_path = os.environ.get("FAKE_ARGS")
if args_path:
    with open(args_path, "w") as stream:
        json.dump(sys.argv[1:], stream)
api_key = os.environ.get("FAKE_API_KEY", "none")
print(json.dumps({"type": "system", "subtype": "init", "session_id": "init-sid",
                  "model": "fake", "apiKeySource": api_key, "mcp_servers": []}), flush=True)

def interrupted(_signum, _frame):
    sys.exit(0)

signal.signal(signal.SIGINT, interrupted)
for line in sys.stdin:
    request = json.loads(line)
    text = request["message"]["content"][0]["text"]
    if text == "block":
        while True:
            signal.pause()
    print("not json", flush=True)
    if text == "fallback":
        result = "First sentence. Second sentence."
    else:
        result = "Done <voice>Changed the files and need review.</voice>"
    permission_denials = []
    terminal_reason = "end_turn"
    if text == "permission denied":
        permission_denials = [{
            "tool_name": "Write",
            "tool_use_id": "toolu_test",
            "reason": "requires_approval"
        }]
        terminal_reason = "completed"
    print(json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Read", "input": {"file_path": "a.txt"}},
        {"type": "tool_use", "name": "Edit", "input": {"file_path": "b.txt"}},
        {"type": "tool_use", "name": "Write", "input": {"file_path": "c.txt"}},
        {"type": "tool_use", "name": "Bash", "input": {"command": "git status"}}
    ]}}), flush=True)
    print(json.dumps({"type": "result", "result": result,
                      "terminal_reason": terminal_reason, "stop_reason": "end_turn",
                      "is_error": False, "permission_denials": permission_denials, "num_turns": 1,
                      "session_id": "sid-1", "usage": {}, "total_cost_usd": 0,
                      "modelUsage": {}, "duration_ms": 1}), flush=True)
    if text == "truncated":
        sys.stdout.write("{truncated")
        sys.stdout.flush()
"""


def test_fake_cli_fixture_syntax_is_valid():
    """Verify the FAKE_CLI embedded script is valid Python."""
    ast.parse(FAKE_CLI)


def _manager(tmp_path, monkeypatch, api_key="none"):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executable = tmp_path / "fake-cli"
    executable.write_text(FAKE_CLI)
    os.chmod(executable, executable.stat().st_mode | 0o111)
    monkeypatch.setenv("FAKE_API_KEY", api_key)
    monkeypatch.setenv("EMBER_GIT_USER_NAME", "Test User")
    monkeypatch.setenv("EMBER_GIT_USER_EMAIL", "test@example.invalid")
    manager = shim.ClaudeProcess(str(workspace), str(executable))
    monkeypatch.setattr(manager, "_configure_git", lambda: None)
    return manager


def test_turn_extracts_voice_activity_and_tolerates_malformed_json(
    tmp_path, monkeypatch
):
    manager = _manager(tmp_path, monkeypatch)
    record = manager.turn("make changes")
    assert record["terminal_reason"] == "end_turn"
    assert record["voice"] == "Changed the files and need review."
    assert record["activity"] == [
        {"type": "tool_use", "name": "Read", "input": {"file_path": "a.txt"}},
        {"type": "edit", "file_path": "b.txt"},
        {"type": "write", "file_path": "c.txt"},
        {"type": "bash", "command": "git status"},
    ]
    assert manager.ready()
    manager._close_process(kill=True)


def test_voice_falls_back_to_first_sentence(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    assert manager.turn("fallback")["voice"] == "First sentence."
    manager._close_process(kill=True)


def test_turn_reports_permission_denials(tmp_path, monkeypatch):
    """Verify permission denials are reported, not swallowed."""
    manager = _manager(tmp_path, monkeypatch)
    record = manager.turn("permission denied")
    assert record["permission_denials"] == [
        {
            "tool_name": "Write",
            "tool_use_id": "toolu_test",
            "reason": "requires_approval",
        }
    ]
    manager._close_process(kill=True)


def test_truncated_jsonl_after_result_is_ignored(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    assert manager.turn("truncated")["terminal_reason"] == "end_turn"
    manager._close_process(kill=True)


def test_api_key_source_assertion(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch, api_key="api_key")
    with pytest.raises(shim.StartupError, match="apiKeySource must be none"):
        manager.turn("hello")
    assert not manager.ready()


def test_configure_git_uses_decoded_boot_environment(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    # These are the values guest-init receives from decoded ember.env boot args.
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("EMBER_GIT_USER_NAME", "Boot User")
    monkeypatch.setenv("EMBER_GIT_USER_EMAIL", "boot@example.invalid")
    manager = shim.ClaudeProcess(str(workspace), "unused")
    manager._configure_git()
    assert (
        subprocess.check_output(
            ["git", "config", "--global", "user.name"], text=True
        ).strip()
        == "Boot User"
    )
    assert (
        subprocess.check_output(
            ["git", "config", "--global", "user.email"], text=True
        ).strip()
        == "boot@example.invalid"
    )


def test_activity_ignores_malformed_messages_and_bounds_tool_input():
    activity = shim.activity_from_events(
        [
            {"type": "assistant", "message": None},
            {"type": "assistant", "message": "not an object"},
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Read",
                            "input": {"large": "x" * (shim.MAX_TOOL_INPUT_BYTES + 1)},
                        }
                    ]
                },
            },
        ]
    )
    assert activity == [
        {
            "type": "tool_use",
            "name": "Read",
            "input": "[omitted: tool input exceeds %d bytes]"
            % shim.MAX_TOOL_INPUT_BYTES,
        }
    ]


def test_child_reaper_preserves_managed_exit_status_and_reaps_unmanaged_child():
    previous_handler = signal.getsignal(signal.SIGCHLD)
    shim.install_child_reaper()
    try:
        managed = subprocess.Popen(["bash", "-c", "sleep 0.05; exit 7"])
        shim._managed_child_pids.add(managed.pid)
        assert managed.wait() == 7
        shim._managed_child_pids.discard(managed.pid)

        orphan = subprocess.Popen(["bash", "-c", "exit 0"])
        deadline = time.time() + 2
        while time.time() < deadline:
            shim._reap_orphans()
            try:
                info = os.waitid(
                    os.P_PID, orphan.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT
                )
            except ChildProcessError:
                break
            assert info is None
            time.sleep(0.01)
        else:
            pytest.fail("unmanaged child was not reaped")
    finally:
        signal.signal(signal.SIGCHLD, previous_handler)


def test_interrupt_waits_for_clean_exit(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    errors = []

    def run_turn():
        try:
            manager.turn("block")
        except RuntimeError as exc:
            errors.append(str(exc))

    thread = threading.Thread(target=run_turn)
    thread.start()
    deadline = time.time() + 2
    while manager.process is None and time.time() < deadline:
        time.sleep(0.01)
    outcome = manager.interrupt(timeout=1)
    thread.join(2)
    assert outcome == {
        "terminal_reason": "user_interrupt",
        "killed": False,
        "timeout": False,
    }
    assert manager.process is None
    assert errors


def test_resume_argument_is_used_on_new_process(tmp_path, monkeypatch):
    args_path = tmp_path / "args.json"
    monkeypatch.setenv("FAKE_ARGS", str(args_path))
    manager = _manager(tmp_path, monkeypatch)
    manager.turn("first")
    first_args = json.loads(args_path.read_text())
    assert "--resume" not in first_args
    verbose_index = first_args.index("--verbose")
    assert first_args[verbose_index : verbose_index + 3] == [
        "--verbose",
        "--permission-mode",
        "bypassPermissions",
    ]
    manager._close_process(kill=True)
    manager.turn("second", session_id="init-sid")
    second_args = json.loads(args_path.read_text())
    assert second_args[second_args.index("--resume") + 1] == "init-sid"
    manager._close_process(kill=True)


def test_permission_mode_can_be_overridden(tmp_path, monkeypatch):
    args_path = tmp_path / "args.json"
    monkeypatch.setenv("FAKE_ARGS", str(args_path))
    monkeypatch.setenv(shim.PERMISSION_MODE_ENV, "default")
    manager = _manager(tmp_path, monkeypatch)
    manager.turn("first")
    args = json.loads(args_path.read_text())
    assert args[args.index("--permission-mode") + 1] == "default"
    manager._close_process(kill=True)


def test_session_id_mismatch_is_rejected_and_missing_id_resumes(tmp_path, monkeypatch):
    args_path = tmp_path / "args.json"
    monkeypatch.setenv("FAKE_ARGS", str(args_path))
    manager = _manager(tmp_path, monkeypatch)
    manager.turn("first")
    manager.turn("matching", session_id="init-sid")
    with pytest.raises(shim.SessionConflictError, match="does not match"):
        manager.turn("wrong", session_id="other-sid")
    manager._close_process(kill=True)
    manager.turn("resume without id")
    args = json.loads(args_path.read_text())
    assert args[args.index("--resume") + 1] == "init-sid"
    manager._close_process(kill=True)


def _run_server(manager):
    server = ThreadingHTTPServer(("127.0.0.1", 0), shim.make_handler(manager))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _request(server, method, path, body=None):
    connection = HTTPConnection("127.0.0.1", server.server_port)
    connection.request(method, path, body=body)
    response = connection.getresponse()
    return response.status, json.loads(response.read())


def test_http_health_ready_and_turn_gating(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    server = _run_server(manager)
    try:
        assert _request(server, "GET", shim.HEALTHZ_PATH)[0] == 200
        assert _request(server, "GET", shim.READY_PATH)[0] == 200
        status, body = _request(server, "POST", shim.TURN_PATH, b"{}")
        assert status == 400 and "empty" in body["error"]
        status, body = _request(
            server, "POST", shim.TURN_PATH, json.dumps({"message": "hi"}).encode()
        )
        assert status == 200 and body["session_id"] == "sid-1"
        assert _request(server, "GET", shim.READY_PATH)[0] == 200
    finally:
        server.shutdown()
        server.server_close()
        manager._close_process(kill=True)


def test_http_rejects_session_conflict_and_bad_content_length(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    server = _run_server(manager)
    try:
        status, _ = _request(
            server, "POST", shim.TURN_PATH, json.dumps({"message": "first"}).encode()
        )
        assert status == 200
        status, body = _request(
            server,
            "POST",
            shim.TURN_PATH,
            json.dumps({"message": "wrong", "session_id": "other-sid"}).encode(),
        )
        assert status == 409 and "does not match" in body["error"]
        connection = HTTPConnection("127.0.0.1", server.server_port)
        connection.request(
            "POST", shim.TURN_PATH, body=b"{}", headers={"Content-Length": "bad"}
        )
        response = connection.getresponse()
        assert response.status == 400
        response.read()
    finally:
        server.shutdown()
        server.server_close()
        manager._close_process(kill=True)


def test_http_clock_uses_noded_epoch_milliseconds(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    server = _run_server(manager)
    values = []
    monkeypatch.setattr(
        shim.time, "clock_settime", lambda clock, value: values.append((clock, value))
    )
    try:
        status, body = _request(server, "POST", shim.CLOCK_PATH, b"1760000000123")
        assert status == 200 and body["epoch_ms"] == 1760000000123
        assert values == [(shim.time.CLOCK_REALTIME, 1760000000.123)]
        assert _request(server, "POST", shim.CLOCK_PATH, b"invalid")[0] == 400
    finally:
        server.shutdown()
        server.server_close()


def test_cli_crash_is_422(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    original = manager._spawn

    def crash_spawn(session_id=None):
        raise RuntimeError("claude crashed during turn, exit code 7")

    manager._spawn = crash_spawn
    server = _run_server(manager)
    try:
        status, body = _request(
            server, "POST", shim.TURN_PATH, json.dumps({"message": "hi"}).encode()
        )
        assert status == 422 and "crashed" in body["error"]
    finally:
        manager._spawn = original
        server.shutdown()
        server.server_close()
